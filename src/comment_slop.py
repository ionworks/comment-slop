#!/usr/bin/env python3
"""Report agent-authored comment slop on lines changed since HEAD."""

from __future__ import annotations

import argparse
import ast
import bisect
from collections.abc import Callable
from dataclasses import dataclass, replace
import io
import itertools
import json
import os
import re
import subprocess
import sys
import tokenize
import warnings

MAX_WORDS = 12
MAX_REPORT = 20
MAX_BYTES = 500_000

# Of a function's lines: numpy p95 27.8%, pandas 25.9%, scipy 33.3%. NOT
# lowered to the tightest corpus -- see docs/calibration.md.
DENSITY_MAX = 0.30
# Floor on the COMMENT side, not the region size. A region-size floor would
# exempt the worst case there is -- a 5-line comment block on a 9-line change.
DENSITY_MIN_COMMENT_LINES = 3

PLAIN, DOC = "plain", "doc"


@dataclass(frozen=True)
class Comment:
    """One comment as its extractor found it, or a run of them merged into a block.

    `lines` holds one entry per PHYSICAL line with the comment markers
    stripped, so a delimiter-only line is an empty entry. `span` and `body`
    derive from it rather than being stored beside it, so no consumer can read a
    span or body that disagrees with the text. Merging concatenates `lines`.

    `mergeable` is False for block forms so they never absorb a neighbouring
    line comment.
    """

    line: int
    col: int
    lines: tuple[str, ...]
    kind: str = PLAIN
    mergeable: bool = True
    # The `Lang.name` of its file. Travels with the comment so production and
    # every measurement path agree on what fires.
    lang: str = ""
    # Shares its line with code. Set once in `extract`, like `lang`, so the
    # merge, the tiers and the density ratio all read one answer.
    inline: bool = False

    @property
    def span(self) -> int:
        return len(self.lines)

    @property
    def body(self) -> str:
        return " ".join(s.strip() for s in self.lines if s.strip()).strip()


def _py_line_comments(src: str) -> list[Comment]:
    """Every `#` comment as a plain one, `#:` included.

    `#:` is the Sphinx/Pydantic attribute-doc marker, and it is stripped like
    any other comment marker rather than earning a kind. A kind of its own, with
    a doc-sized budget and density neutrality, would let a 58-word operational
    narrative on a one-line constant through, and nothing a reader gains from
    `#:` makes the prose after it less of an ordinary comment.

    `##:` is not the convention, so its `#` run is stripped and the `:` it
    leaves behind keeps meeting the downstream veto.
    """
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type != tokenize.COMMENT:
                continue
            text = tok.string
            body = text[2:] if text.startswith("#:") else text.lstrip("#")
            out.append(Comment(tok.start[0], tok.start[1], (body.strip(),)))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return out  # keep what was already tokenised
    return out


def py_docstrings(src: str) -> list[Comment]:
    """Docstrings as `doc` comments.

    An independent failure path from `_py_line_comments`: a file whose `ast`
    parse fails still yields its line comments, because dropping working
    findings on a syntax error would be a regression. Costs 1.3 ms/file.

    Warnings are silenced, because `ast.parse` prints a `SyntaxWarning` for an
    invalid escape like `"\\d"`. The hook wrapper only counts output as findings
    when it OPENS with `comment-slop:`, so a warning printed ahead of a report
    would make it exit 0 and drop that report.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return []
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        text = first.value.value.strip()
        end = first.end_lineno or first.lineno
        lines = tuple(seg.strip() for seg in text.split("\n"))
        # The physical span includes the closing delimiter line; pad so
        # len(lines) == span and the verbose tier counts physical lines.
        span = end - first.lineno + 1
        lines = lines + ("",) * max(0, span - len(lines))
        out.append(
            Comment(
                first.lineno,
                first.col_offset,
                lines[:span] or ("",),
                DOC,
                mergeable=False,
            )
        )
    return out


def py_comments(src: str) -> list[Comment]:
    return sorted(
        _py_line_comments(src) + py_docstrings(src), key=lambda c: (c.line, c.col)
    )


def _locator(src: str) -> Callable[[int], tuple[int, int]]:
    """`at(i)`: the 1-based line and 0-based column of offset `i` in `src`.

    The hand-written lexers track offsets only and ask this where a comment
    starts, so none keeps a line counter of its own. A counter has to see every
    newline it skips over: miss one under an escaped newline and every later
    finding points a line above itself.
    """
    starts = [0, *itertools.accumulate(len(ln) + 1 for ln in src.split("\n"))]

    def at(i: int) -> tuple[int, int]:
        line = bisect.bisect_right(starts, i)
        return line, i - starts[line - 1]

    return at


def _strip_stars(inner: str) -> tuple[str, ...]:
    """One entry per physical line of a /* */ body, leading `*` removed."""
    return tuple(seg.strip().lstrip("*!").strip() for seg in inner.split("\n"))


# `/` after these starts a regex, not a division. Read as division, a `"`
# inside the pattern opens a string that swallows every later comment.
REGEX_KEYWORDS = frozenset(
    {
        "return",
        "typeof",
        "case",
        "in",
        "of",
        "new",
        "delete",
        "void",
        "instanceof",
        "do",
        "else",
        "yield",
        "await",
        "throw",
    }
)
# `<` after these opens an element, not a comparison. Same question the regex
# branch asks, so they share `_expression_position`.
JSX_KEYWORDS = REGEX_KEYWORDS | frozenset({"default"})


def _preceding_word(src: str, i: int) -> str:
    """The identifier ending just before `i`, ignoring intervening whitespace."""
    j = i
    while j > 0 and src[j - 1].isspace():
        j -= 1
    end = j
    while j > 0 and (src[j - 1].isalnum() or src[j - 1] in "_$"):
        j -= 1
    return src[j:end]


def _expression_position(
    src: str, i: int, prev_sig: str, keywords: frozenset[str]
) -> bool:
    """Whether `src[i]` opens a value rather than continuing one.

    A `/` or `<` after a value -- an identifier, a literal, a closing bracket --
    is an operator. After anything else it opens a regex literal or a JSX
    element. `keywords` covers the case the previous character cannot: `return`
    ends in an identifier character but is not a value.
    """
    if prev_sig in ")]}":
        return False
    return (
        not (prev_sig.isalnum() or prev_sig in "_$'\"`")
        or _preceding_word(src, i) in keywords
    )


# The only characters that can end a string or regex; the run between them is
# skipped in one C-level search. No `\n` for templates, which may span lines.
_STR_STOP = {q: re.compile(rf"[\\\n{re.escape(q)}]") for q in "'\""}
# `${` as well: a template substitution is code, and a comment can live in it.
_STR_STOP["`"] = re.compile(r"[\\`]|\$\{")
_RE_STOP = re.compile(r"[\\\n\[\]/]")


def _js_string_end_from(src: str, i: int) -> int:
    """Scan template text starting at `i`, which is *inside* a literal.

    Returns the index of the closing backtick's terminator or of the next
    `${`. Unlike `_js_string_end` there is no opening quote at `i` to read,
    because a substitution just closed and the text resumes mid-literal.
    """
    n = len(src)
    j = i
    while j < n:
        m = _STR_STOP["`"].search(src, j)
        if m is None:
            return n
        j = m.start()
        if src[j] == "\\":
            j += 2
            continue
        if src[j] == "$":
            return j
        return j + 1  # the closing backtick
    return n


def _js_string_end(src: str, i: int) -> int:
    """The index just past the string opening at `src[i]`.

    A raw newline ends a `'` or `"` string: JS forbids one there, so an
    unterminated quote must not swallow the rest of the file. A template literal
    may span lines.

    A template's `${...}` is code, so the scan stops at the `${` and the caller
    resumes lexing there; `_TPL` on the frame stack is what remembers that the
    matching `}` returns to template text rather than to the enclosing code.
    """
    quote = src[i]
    n = len(src)
    # Jump between the characters that can END the literal, not every one: 9 of
    # a 212KB file's 9.2ms went here, which is why the scan jumps.
    stop = _STR_STOP[quote]
    j = i + 1
    while j < n:
        m = stop.search(src, j)
        if m is None:
            return n
        j = m.start()
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if c == "$":
            return j  # `${`: the caller lexes the substitution as code
        return j  # a raw newline, and this quote may not span lines
    return n


def _js_regex_end(src: str, i: int) -> int:
    """The index just past the regex literal opening at `src[i]`.

    A character class may hold an unescaped `/` (`/[/]/`), and a raw newline
    ends the literal -- JS forbids one there too.
    """
    n = len(src)
    j, in_class = i + 1, False
    while j < n:
        m = _RE_STOP.search(src, j)
        if m is None:
            return n
        j = m.start()
        c = src[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return j
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            return j + 1
        j += 1
    return n


# TypeScript's rule for the `.tsx` ambiguity: `<T>` is an element, `<T extends
# U>` and `<T,>` are type parameters. Without it a generic arrow loses the file.
TYPE_PARAMS = re.compile(r"<(?:const\s+)?[A-Za-z_$][\w$]*\s*(?:,|extends\b)")

# Frame kinds for the JSX scanner. CODE is JavaScript, TAG is inside `<...>`,
# TEXT is the children region between a tag's `>` and its closing `</`.
_CODE, _TAG, _TEXT, _TPL = "code", "tag", "text", "tpl"


def js_comments(src: str, jsx: bool = True) -> list[Comment]:
    """`//` and `/* */`, skipping strings, regex literals and JSX text.

    JSX children are literal text, not code: read as code, a shell snippet in a
    `<Box component="pre">` has its `/*` taken for a comment, inventing a
    56-line one.

    The frame stack is the mechanism: `{` in a tag or text pushes CODE back on.

    `jsx` is False for `.ts`/`.mts`/`.cts`, where TypeScript forbids JSX so
    `<Foo>x` stays a type assertion instead of eating the file as text.

    Unbalanced JSX leaves a TEXT frame open, silencing the rest -- the safe
    direction, as for an unterminated heredoc.
    """
    out: list[Comment] = []
    at = _locator(src)
    n = len(src)
    i = 0
    prev_sig = ""
    # [kind, brace_depth]; only CODE frames use the depth.
    frames: list[list] = [[_CODE, 0]]

    while i < n:
        c = src[i]
        kind = frames[-1][0]

        # `{` in a tag or in text opens an embedded expression. One case for
        # both frames, which is why `{/* ... */}` needs no case of its own.
        if kind != _CODE and c == "{":
            frames.append([_CODE, 0])
            i += 1
            continue

        # Template text between substitutions. Only `${` (code) and the closing
        # backtick matter; everything else is literal, `//` and `/*` included.
        if kind == _TPL:
            end = _js_string_end_from(src, i)
            if end < n and src[end : end + 2] == "${":
                i = end + 2
                frames.append([_CODE, 0])
                continue
            i = end
            frames.pop()
            prev_sig = "`"
            continue

        if kind == _TEXT:
            if c == "<" and i + 1 < n:
                if src[i + 1] == "/":
                    end = src.find(">", i)
                    i = n if end == -1 else end + 1
                    frames.pop()
                    prev_sig = ")"  # an element is a value
                    continue
                # A tag name or `<>`; anything else is text, since prose says
                # `5 < 6`. As a tag it would push a TEXT frame `</Box>` can't pop.
                if src[i + 1].isalpha() or src[i + 1] in "_$>":
                    frames.append([_TAG, 0])
                    i += 1
                    continue
            i += 1  # literal text
            continue

        # CODE and TAG both hold real comments; `<Foo /* why */ bar>` is legal.
        # One `/` dispatch for line, block and (in CODE) a regex literal.
        nxt = src[i + 1] if c == "/" and i + 1 < n else ""
        if nxt == "/":
            end = src.find("\n", i)
            end = n if end == -1 else end
            # `//!` is the bundler preserve-banner convention, not prose. The
            # Rust lexer keeps its `//!` -- there it is inner documentation.
            if not src[i:end].startswith("//!"):
                out.append(Comment(*at(i), (src[i + 2 : end].strip(),)))
            i = end
            continue
        if nxt == "*":
            end = src.find("*/", i + 2)
            closed = end != -1
            end = n if not closed else end + 2
            text = src[i:end]
            inner = text[2:-2] if closed else text[2:]
            # `/**` is JSDoc; `/***` is not, by typedoc convention.
            is_doc = text.startswith("/**") and not text.startswith("/***")
            out.append(
                Comment(
                    *at(i),
                    _strip_stars(inner),
                    DOC if is_doc else PLAIN,
                    mergeable=False,
                )
            )
            i = end
            prev_sig = "/"
            continue

        if kind == _TAG:
            if c == "/" and src[i + 1 : i + 2] == ">":
                frames.pop()  # self-closing: no children region
                prev_sig = ")"
                i += 2
                continue
            if c == ">":
                frames[-1] = [_TEXT, 0]
                i += 1
                continue
            if c in "'\"":
                i = _js_string_end(src, i)
                continue
            i += 1
            continue

        # Everything below is reached only in a CODE frame, so the string,
        # brace, element and regex cases need no kind test of their own.
        if c in "'\"`":
            end = _js_string_end(src, i)
            if c == "`" and end < n and src[end : end + 2] == "${":
                i = end + 2
                frames.append([_TPL, 0])
                frames.append([_CODE, 0])
                continue
            i = end
            prev_sig = c
            continue
        if c == "{":
            frames[-1][1] += 1
            prev_sig = c
            i += 1
            continue
        if c == "}":
            if frames[-1][1] == 0 and len(frames) > 1:
                frames.pop()  # closes a `{...}` opened in a tag, text or `${`
                prev_sig = ")"
            else:
                frames[-1][1] = max(0, frames[-1][1] - 1)
                prev_sig = c
            i += 1
            continue
        # Rejects the second half of `1 << e`, which reads as expression
        # position. A shift cannot carry a space, so the test is exact.
        if (
            jsx
            and c == "<"
            and i + 1 < n
            and src[i - 1 : i] != "<"
            and (src[i + 1].isalpha() or src[i + 1] in "_$>")
            and not TYPE_PARAMS.match(src, i)
            and _expression_position(src, i, prev_sig, JSX_KEYWORDS)
        ):
            frames.append([_TAG, 0])
            i += 1
            continue
        if nxt and _expression_position(src, i, prev_sig, REGEX_KEYWORDS):
            i = _js_regex_end(src, i)
            prev_sig = "/"
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return out


# YAML 1.2.2 8.1.1: `|` or `>`, then an optional indentation indicator (1-9)
# and an optional chomping indicator (+ or -), in either order.
BLOCK_HEADER = re.compile(r"[|>](?:[1-9][-+]?|[-+][1-9]?)?$")


def yaml_comments(src: str) -> list[Comment]:
    """`#` outside quotes, skipping block-scalar bodies.

    A block scalar's body is literal text, so a `#` in it is content. The body
    is whatever is indented deeper than the header line, and a blank line
    inside it must not end the scope -- a blank line has no indentation, so
    comparing indents alone would end the scalar early and read the rest as
    comments.

    Accepted limitation: multi-line quoted scalars.
    """
    out: list[Comment] = []
    skip_indent: int | None = None
    for lineno, line in enumerate(src.split("\n"), 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if skip_indent is not None:
            if not stripped or indent > skip_indent:
                continue
            skip_indent = None
        # One quote-aware pass: the content before the comment decides whether
        # a block scalar opens. Header-first would swallow a real comment.
        cut, i, quote = None, 0, None
        while i < len(line):
            c = line[i]
            if quote:
                if c == "\\" and quote == '"':
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "#" and (i == 0 or line[i - 1] in " \t"):
                cut = i
                break
            i += 1
        content = (line[:cut] if cut is not None else line).rstrip()
        head = content.lstrip()
        if head and not head.startswith("#"):
            m = BLOCK_HEADER.search(content)
            # A header is a value, not a plain scalar that happens to end in
            # `>`: `cmd: echo >` is text, `cmd: |2-` is a header.
            if m and (
                m.start() == len(content) - len(head)
                or content[: m.start()].rstrip()[-1:] in ":-"
            ):
                skip_indent = indent
        if cut is not None:
            out.append(Comment(lineno, cut, (line[cut + 1 :].strip(),)))
    return out


def toml_comments(src: str) -> list[Comment]:
    """`#` outside a string, across TOML's four string forms.

    Not YAML's scanner with the extensions swapped. Multi-line `\"\"\"`/`'''`
    carry quote state ACROSS lines, so the scan cannot be per-line; YAML calls
    multi-line quoted scalars an accepted limitation because they are rare
    there, but a `\"\"\"description\"\"\"` is ordinary TOML and a `#` inside one
    would be reported as prose the author never wrote.

    And `#` needs no preceding whitespace -- bare keys cannot contain one, so
    `x = 1# c` is a comment where YAML's `a#b` is a scalar. Escapes exist only
    in the `\"` forms.
    """
    out: list[Comment] = []
    at = _locator(src)
    n = len(src)
    i = 0
    while i < n:
        c = src[i]
        if c in "\"'":
            if src[i : i + 3] == c * 3:
                close, j = c * 3, i + 3
                while j < n:
                    if c == '"' and src[j] == "\\":
                        j += 2
                        continue
                    if src.startswith(close, j):
                        j += 3
                        # Up to two quotes may sit against the delimiter, so
                        # `\"\"\"he said \"hi\"\"\"\"` closes at the LAST of the run.
                        while j < n and src[j] == c:
                            j += 1
                        break
                    j += 1
                i = min(j, n)
                continue
            # A single-line string cannot hold a newline, so an unterminated
            # one ends at it rather than consuming the rest of the file.
            j = i + 1
            while j < n and src[j] != "\n":
                if c == '"' and src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    j += 1
                    break
                j += 1
            i = min(j, n)
            continue
        if c == "#":
            end = src.find("\n", i)
            end = n if end == -1 else end
            out.append(Comment(*at(i), (src[i + 1 : end].strip(),)))
            i = end
            continue
        i += 1
    return out


# `<<EOF`, `<<-EOF`, `<<"EOF"`. Only on the instructions that can carry a
# heredoc, so a `<<` inside an ordinary argument cannot open one.
HEREDOC_OWNER = re.compile(r"^\s*(RUN|COPY|ADD)\b", re.I)
HEREDOC = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")

# `# syntax=docker/dockerfile:1` -- the builder reads it, so it is the
# Dockerfile spelling of the lint directives NEVER exempts everywhere else.
DIRECTIVE = re.compile(r"^(syntax|escape|check)\s*=", re.I)


def dockerfile_comments(src: str) -> list[Comment]:
    """`#` as the first non-space character of a line, and nowhere else.

    Not YAML's scanner renamed. A Dockerfile has no trailing comment form --
    everything after the instruction is its argument -- so YAML's rule would
    report `RUN curl x/#frag` and `LABEL n="a # b"` as prose nobody wrote.

    A heredoc body is literal script, skipped for the reason YAML skips a block
    scalar. An unterminated delimiter therefore silences the rest of the file:
    the safe direction, and what tree-sitter reads too. A `\\` continuation
    needs no case -- the builder strips comment lines inside one either way.
    """
    out: list[Comment] = []
    pending: list[str] = []  # heredoc delimiters awaiting their terminator
    for lineno, line in enumerate(src.split("\n"), 1):
        if pending:
            # `<<-` permits a tab-indented terminator, so compare stripped.
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        head = line.lstrip()
        if head.startswith("#"):
            # `## Section` is the common banner form; left in the body, the
            # second marker would hide it from the tiers that read the first word.
            body = head.lstrip("#").strip()
            # A directive is not prose, and a build's opening explanation sits
            # right under one. Merged, the veto on the directive would silence it.
            out.append(
                Comment(
                    lineno,
                    len(line) - len(head),
                    (body,),
                    mergeable=not DIRECTIVE.match(body),
                )
            )
            continue
        if HEREDOC_OWNER.match(line):
            pending = [m.group(2) for m in HEREDOC.finditer(line)]
    return out


# `<<EOF`, `<<-EOF`, `<<"EOF"`, `<<\EOF`, `<< EOF`. `<<<` is a herestring, whose
# operand is an ordinary word, so the lookahead keeps it from opening a body.
SH_HEREDOC = re.compile(r"<<-?(?!<)\s*\\?([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")

# A `#` opens a comment only at a word start (`${x#pre}` and `it#not` are
# words). Checked against bash: even `>#` opens one, so `>#hash` is an error.
SH_WORD_BREAK = frozenset(" \t\n;|&()<>`")


def shell_comments(src: str) -> list[Comment]:
    """`#` at the start of a word, outside quotes and heredoc bodies.

    Not `dockerfile_comments` with more extensions. Shell has a trailing
    comment form (`set -e  # why`), quoting that spans lines, and `$'...'`, so
    the scan has to carry state across lines the way `toml_comments` does
    rather than decide each line on its own.

    A heredoc body is literal script, skipped for the reason a Dockerfile's is;
    an unterminated delimiter therefore silences the rest of the file, which is
    the safe direction. `<<<` is excluded because a herestring has no body.
    """
    out: list[Comment] = []
    at = _locator(src)
    n = len(src)
    i = 0
    pending: list[str] = []  # heredoc delimiters awaiting their terminator

    while i < n:
        c = src[i]
        if c == "\n":
            i += 1
            while pending and i < n:
                end = src.find("\n", i)
                end = n if end == -1 else end
                # `<<-` permits a tab-indented terminator, so compare stripped.
                if src[i:end].strip() == pending[0]:
                    pending.pop(0)
                i = end + 1 if end < n else n
            continue
        if c == "\\":
            i = min(i + 2, n)
            continue
        if c == "'":
            end = src.find("'", i + 1)
            i = n if end == -1 else end + 1
            continue
        # `$'...'` is the one quote where a backslash escapes inside single
        # quotes; `$"..."` needs no case, since the `"` below reads it.
        if c == '"' or (c == "$" and src[i + 1 : i + 2] == "'"):
            quote = c if c == '"' else "'"
            j = i + 2 if c == "$" else i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            i = min(j, n)
            continue
        if src[i : i + 2] == "<<":
            m = SH_HEREDOC.match(src, i)
            if m:
                pending.append(m.group(2))
                i = m.end()
                continue
            i += 2
            continue
        # A newline is a word break, so this covers the start of every line.
        if c == "#" and (i == 0 or src[i - 1] in SH_WORD_BREAK):
            end = src.find("\n", i)
            end = n if end == -1 else end
            # `## Section` is a common banner form, and the second marker
            # hides it from the tiers that read the first word.
            out.append(Comment(*at(i), (src[i:end].lstrip("#").strip(),)))
            i = end
            continue
        i += 1
    return out


def rust_comments(src: str) -> list[Comment]:
    """Rust comments, distinguishing doc forms from ordinary ones.

    Four things separate this from the JS lexer: block comments nest, so the
    closing delimiter needs depth counting rather than a find; raw strings
    (`r#"..."#`, `br#"..."#`, `cr#"..."#`) suspend every escape while non-raw
    byte and C strings (`b"..."`, `c"..."`) do not; `'a` is a lifetime, not the
    start of a char literal, which a naive lexer treats as unterminated and
    then consumes the rest of the file.

    Per rustc, `////` and `/**/` are ordinary comments, not doc forms.
    """
    out: list[Comment] = []
    at = _locator(src)
    n = len(src)
    i = 0
    while i < n:
        c = src[i]
        # A raw string suspends escapes; b"..." and c"..." (Rust 1.77) do not,
        # and an identifier ending in r, b or c is not a prefix at all.
        if c in "rbc" and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            j = i + 1
            if c in "bc" and j < n and src[j] == "r":
                j += 1
            if (c == "r" or j > i + 1) and j < n and src[j] in '#"':
                hashes = 0
                while j < n and src[j] == "#":
                    hashes, j = hashes + 1, j + 1
                if j < n and src[j] == '"':
                    close = '"' + "#" * hashes
                    end = src.find(close, j + 1)
                    i = n if end == -1 else end + len(close)
                    continue
            if c in "bc" and j < n and src[j] == '"':
                i = j  # consume the prefix; the " handler does the rest
                continue
        if c == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                j += 1
            i = j
            continue
        if c == "'":
            if i + 1 < n and src[i + 1] == "\\":
                j = i + 2
                while j < n and src[j] not in "'\n":
                    j += 1
                i = min(j + 1, n)
                continue
            if i + 2 < n and src[i + 2] == "'":
                i += 3
                continue
            i += 1  # a lifetime or a loop label
            continue
        if c == "/" and i + 1 < n:
            if src[i + 1] == "/":
                end = src.find("\n", i)
                end = n if end == -1 else end
                text = src[i:end]
                is_doc = (
                    text.startswith("///") and not text.startswith("////")
                ) or text.startswith("//!")
                out.append(
                    Comment(
                        *at(i),
                        (text.lstrip("/").lstrip("!").strip(),),
                        DOC if is_doc else PLAIN,
                    )
                )
                i = end
                continue
            if src[i + 1] == "*":
                is_doc = src[i : i + 3] == "/*!" or (
                    src[i : i + 3] == "/**" and src[i : i + 4] not in ("/***", "/**/")
                )
                depth, j = 1, i + 2
                while j < n and depth:
                    if src[j : j + 2] == "/*":
                        depth, j = depth + 1, j + 2
                        continue
                    if src[j : j + 2] == "*/":
                        depth, j = depth - 1, j + 2
                        continue
                    j += 1
                text = src[i:j]
                inner = text[2:-2] if text.endswith("*/") else text[2:]
                out.append(
                    Comment(
                        *at(i),
                        _strip_stars(inner),
                        DOC if is_doc else PLAIN,
                        mergeable=False,
                    )
                )
                i = j
                continue
        i += 1
    return out


# `uses: owner/repo@<sha>  # v6.2.0` -- the tag survives nowhere else on the
# line, so the annotation is the convention rather than a label for it.
SHA_PIN = re.compile(
    r"^(?:pin(?:ned)?\s+(?:to\s+)?)?"
    r"(?:v\d+(?:\.\d+){0,3}|\d+(?:\.\d+){1,3})"
    r"(?:[.+-][A-Za-z0-9.+-]+)?"
    r"(?:\s*[(,-]?\s*sha[- ]?pin(?:ned)?\)?)?$",
    re.I,
)

# Config files disable a setting by commenting it out, not deleting it, so a
# run of `key =` lines under one explanatory line reads as long prose.
CONFIG_LINE = re.compile(
    r"""^\s*(\[\[?[A-Za-z0-9_."'-]+\]\]?\s*$   # [table], [[array of tables]]
        |["'A-Za-z0-9_.-]+\s*=                 # key =
        |["'][^"']*["']\s*,?$                  # a lone list element
        |[\]}],?\s*$)""",
    re.X,
)


def commented_out_config(lines: tuple[str, ...]) -> bool:
    """Whether a block is mostly disabled configuration rather than prose."""
    text = [s for s in lines if s.strip()]
    if not text:
        return False
    return sum(1 for s in text if CONFIG_LINE.match(s)) >= max(1, len(text) // 2)


@dataclass(frozen=True)
class Lang:
    """Everything the engine knows about one language, in one record.

    Adding a language is one entry in `LANGUAGES` plus its lexer: every
    per-language fact the engine reads lives in this record.
    """

    name: str
    extract: Callable[[str], list[Comment]]
    exts: frozenset[str]
    # Languages that name the file instead of suffixing it, like `Dockerfile`.
    stems: frozenset[str] = frozenset()
    # Tier names that never fire here.
    suppressed: frozenset[str] = frozenset()
    # Exempts a single comment outright: it fires nothing, merges with nothing,
    # and leaves the density numerator.
    veto: re.Pattern[str] | None = None
    # Exempts a merged block, judged on its classified lines. Unlike `veto` it
    # leaves merging alone, because it needs the whole run to decide.
    block_veto: Callable[[tuple[str, ...]], bool] | None = None
    # DENSITY_MAX is a percentile of a Python FUNCTION, while a script's scope
    # is its whole file.
    density_max: float = DENSITY_MAX


LANGUAGES = {
    lang.name: lang
    for lang in (
        Lang("python", py_comments, frozenset({".py", ".pyi"})),
        # Every file where JSX is legal. Babel allows it in `.js`.
        Lang("jsx", js_comments, frozenset({".js", ".jsx", ".mjs", ".cjs", ".tsx"})),
        # TypeScript forbids JSX in `.ts` so that `<Foo>x` can stay a type
        # assertion. See `js_comments`.
        Lang(
            "ts",
            lambda src: js_comments(src, jsx=False),
            frozenset({".ts", ".mts", ".cts"}),
        ),
        Lang("yaml", yaml_comments, frozenset({".yml", ".yaml"}), veto=SHA_PIN),
        Lang(
            "toml",
            toml_comments,
            frozenset({".toml"}),
            # A lone name in a lint-ignore list is an opaque code, not a label:
            # `bare-identifier` is wrong on 15 of 15.
            suppressed=frozenset({"bare-identifier"}),
            block_veto=commented_out_config,
        ),
        Lang("rust", rust_comments, frozenset({".rs"})),
        Lang(
            "dockerfile",
            dockerfile_comments,
            frozenset({".dockerfile", ".containerfile"}),
            stems=frozenset({"dockerfile", "containerfile"}),
            veto=DIRECTIVE,
        ),
        # Extensions only: `language_of` reads a path, not the source, so a
        # shebang is invisible to it.
        Lang(
            "shell",
            shell_comments,
            frozenset({".sh", ".bash", ".zsh", ".ksh"}),
            # Whole-script p95 is 38.5% (vcpkg); see docs/calibration.md.
            density_max=0.40,
        ),
    )
}
_BY_EXT = {ext: lang for lang in LANGUAGES.values() for ext in lang.exts}
_BY_STEM = {stem: lang for lang in LANGUAGES.values() for stem in lang.stems}


def language_of(path: str) -> Lang | None:
    """The language `path` is written in, or None if no lexer reads it.

    A Dockerfile names its language instead of suffixing it: `Dockerfile`,
    `Dockerfile.prod` and `Containerfile` all have a suffix `os.path.splitext`
    either cannot see or reads as the variant. A real supported extension still
    wins, so `Dockerfile.py` stays Python rather than losing every docstring.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _BY_EXT:
        return _BY_EXT[ext]
    return _BY_STEM.get(os.path.basename(path).lower().split(".", 1)[0])


def extract(src: str, path: str) -> list[Comment]:
    """Comments for `path`, or [] if unsupported or the lexer failed.

    A defect in one lexer degrades that file to "no findings" rather than
    propagating: the hook must never break the edit loop.

    `lang` and `inline` are stamped on here, and the shebang dropped, so every
    consumer reads one answer. The shebang -- line 1, column 0 of a `#!` file --
    is an interpreter directive, not prose: it merges with nothing, reaches no
    tier and counts as code in the density ratio. The lexers keep it, as
    tree-sitter does.
    """
    lang = language_of(path)
    if lang is None:
        return []
    try:
        raw = lang.extract(src)
    except Exception:
        return []
    lines = src.split("\n")
    shebang = src.startswith("#!")
    return [
        replace(
            c,
            lang=lang.name,
            inline=c.line <= len(lines) and bool(lines[c.line - 1][: c.col].strip()),
        )
        for c in raw
        if not (shebang and c.line == 1 and c.col == 0)
    ]


# NEVER is case-insensitive except the issue key: folded, `us-east-1` would read
# as one, and `(?<![\w-])` stops `US-EAST-1` matching through its `EAST-1` tail.
NEVER = re.compile(
    r"""
    noqa | type:\s*ignore | pylint: | pyright: | mypy: | ruff: | flake8:
  | eslint- | prettier-ignore | biome-ignore | @ts- | c8\s+ignore | istanbul\s+ignore
  | pragma | fmt:\s*(on|off) | yapf: | isort: | shellcheck
  # Uppercase only, like the ticket-ID alternative below. These are ordinary
  # English words too, and case-insensitively `a real lexer bug sat here` would
  # read as a `BUG:` marker, and a veto is a miss no tier can report.
  | (?-i:\bTODO\b | \bFIXME\b | \bHACK\b | \bXXX\b | \bBUG\b | \bWARNING\b
  | \bSAFETY\b | \bSECURITY\b | \bNOTE\b | \bIMPORTANT\b | \bCAUTION\b)
  | copyright | spdx-license | @license | @preserve | @flow | \beslint\b
  | https?:// | (?-i:(?<![\w-])[A-Z][A-Z0-9]+-\d+\b) | \bRFC\s?\d+ | \#\d+
    """,
    re.IGNORECASE | re.VERBOSE,
)

STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "of",
    "for",
    "in",
    "on",
    "at",
    "by",
    "and",
    "or",
    "if",
    "is",
    "are",
    "be",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "we",
    "our",
    "you",
    "your",
    "i",
    "from",
    "with",
    "as",
    "into",
    "then",
    "all",
    "each",
    "any",
    "so",
    "do",
    "does",
    "was",
    "will",
    "can",
    "here",
}

# Closed vocabulary: the ways to say "I just edited this" enumerate. Without
# `rework`/`tweak` it catches 4 of 6. Ordered before REASON's tiers.
#
# Completed-edit forms only: an imperative is how a docstring summary reads, so
# `updated?`/`drop(ped)?` would cost 2 of the corpus's 6 false positives.
EDIT_NARRATION = re.compile(
    r"^\(?\s*(added|adding|updated|fixe[ds]|changed|modified|"
    r"removed|renamed|refactor(ed|ing)|rework(ed|ing)|tweak(ed|ing)|"
    r"adjust(ed|ing)|simplified|clean(ed) up|dropped|switched|"
    r"moved|inlined|extracted|renaming|reverted|"
    r"was|previously|instead of)\b"
    r"|^\(?\s*(fix|new|add|update|change|remove|drop|move|split|"
    r"refactor|rework|revert)\s*:",
    re.I,
)
BANNER = re.compile(
    r"^[=\-*~_#\s]{3,}$"  # pure divider
    # Labels carry hyphens and dots, which a \w-only class would miss. 120 bounds
    # the regex without reading display width as semantics.
    r"|^[=\-*~_#]{3,}\s*\w[\w\s.,'/-]{0,120}?\s*[=\-*~_#]{3,}\s*$"  # == LABEL ==
)
SECTION_LABEL = re.compile(
    r"^(helpers?|utils?|utilities|constants?|imports?|types?|main|setup|"
    r"config(uration)?|exports?|public api|private|globals?|state|"
    r"variables?|functions?|classes?|interfaces?)\s*[:=\-]*$",
    re.I,
)
# A lone identifier labels the thing below and adds nothing. A dot needs
# identifier characters after it, or "Workspaces." reads as one.
BARE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")
EMOJI = re.compile("[\u2600-\u27bf\U0001f300-\U0001faff\u2705\u274c\u26a0]")
NARRATIVE = re.compile(
    r"^(now|first(ly)?|next|then|finally|lastly)\b|"
    r"^(let'?s|i'?ll|you can)\b",
    re.I,
)
STEP_MARKER = re.compile(r"^(step|test|case|part|phase)\s*\d+\b|^\d+[.)]\s+\w", re.I)
META = re.compile(
    r"\b(for (clarity|readability|safety|completeness|consistency)|"
    r"as requested|to be safe|just in case|self[- ]explanatory|"
    r"as (you )?(can see|mentioned)|obviously|simply)\b",
    re.I,
)


@dataclass
class Finding:
    line: int
    reason: str
    text: str


def words(text: str) -> set[str]:
    out = set()
    for part in re.split(r"[^A-Za-z0-9]+", text):
        if not part:
            continue
        for piece in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+", part):
            w = piece.lower()
            if w and w not in STOPWORDS and len(w) > 1:
                out.add(w)
    return out


def restates(comment: str, code_line: str) -> bool:
    cw = words(comment)
    if not cw or len(cw) > 8:
        return False
    code = re.sub(r"""(["'`]).*?\1""", " ", code_line)
    return cw <= words(code)


DELIM = re.compile(r"^[=\-*~_#\s]+$")


# Raw words; numpy p98=38, pandas p98=35, median 7 here too. Set on 116 labels:
# kept comments top out at 30, and a 35 ceiling catches only 6 of 46 misses.
VERBOSE_WORDS = 30
# Set on 116 human labels: no comment the author kept runs past two lines,
# while 15 of 46 labelled misses are 3 lines or more.
VERBOSE_LINES = 3
# Doc prose is an API contract and carries a bounded explanation. Dogfood put
# useful retry semantics at 29-80 words; the next helper crossed 90.
DOC_VERBOSE_WORDS = 90
DOC_VERBOSE_LINES = 10
RAW_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")

# A comment pointing at a line number rots the moment either file is edited.
CODE_REF = re.compile(
    r"\b[\w./-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|rs|sql|toml|ya?ml|md)\s*:\s*\d+"
    r"|\blines?\s+\d+\s*(?:-|to|\u2013)\s*\d+",
    re.I,
)

# Plan/process vocabulary belongs in the PR, not in the source.
PROCESS_LEAK = re.compile(
    r"\b(task|step|phase|stage|milestone|chunk|part)\s*\d+\b"
    r"|\bper the (plan|spec|design)\b|\bas (planned|specced|designed)\b"
    r"|\bin this (PR|patch|commit|change)\b|\bPR\s*#?\d+",
    re.I,
)

# A snapshot is true of one machine on one day, and nothing re-checks it. The
# unit discriminates, not the digit -- see docs/calibration.md.
PERF_ADJ = (
    r"faster|slower|quicker|cheaper|lighter"
    r"|fewer|smaller|larger|bigger"
    r"|speed-?ups?|slow-?downs?"
)
# `~` is not a word char, so no \b; the lookbehind drops `m8i.2xlarge`.
MAGNITUDE = r"(?<![\w.])~?\d[\d,]*(?:\.\d+)?\s*[kKmMgG]?"
TIME_UNIT = r"(?:[nµu]s|ms|secs?|seconds?|mins?|minutes?|hours?|s)"
SIZE_UNIT = r"(?:[kKmMgGtT]i?[bB]|bytes?)"

BENCHMARK = re.compile(
    # A ratio or percentage against a comparative, either order.
    rf"{MAGNITUDE}\s*(?:[x×]|%)\s*(?:as\s+)?(?:{PERF_ADJ})\b"
    rf"|\b(?:{PERF_ADJ})\s+(?:by\s+)?{MAGNITUDE}\s*(?:[x×]|%)"
    # A duration is a snapshot whatever verb reports it.
    rf"|\b(?:measured|benchmarked|profiled|timed|clocked|took|takes|costs?"
    rf"|ran in|runs in)\b[^.]{{0,40}}?{MAGNITUDE}\s*{TIME_UNIT}\b"
    # A volume often is not, so it needs an explicit measurement verb.
    rf"|\b(?:measured|benchmarked|profiled)\b[^.]{{0,40}}?{MAGNITUDE}\s*"
    rf"{SIZE_UNIT}\b",
    re.I,
)

# A cost carrying an argument is a premise, not a snapshot: deleting the number
# deletes the reason. Both of this tier's hits on this file are of that kind.
BENCHMARK_PREMISE = re.compile(
    r"\bwhich is why\b|\bthat is why\b|\bhence\b"
    r"|\bwhy\b.{0,30}\b(is|are|it)\b",
    re.I,
)


def benchmark(body: str) -> bool:
    return bool(
        BENCHMARK.search(body)
        and not REASON.search(body)
        and not BENCHMARK_PREMISE.search(body)
    )


# What the code used to do is what git is for. The pronoun subject and the two
# lookaheads are measured, not stylistic -- see docs/calibration.md.
HISTORICAL = re.compile(
    r"\b(this|that|it|they|them|these|those|we)\s+used\s+to\b"
    r"|\b(previously|originally|historically|formerly)\b"
    r"(?!-\w+ed\b)(?!\s+\w+ed\s+(?:by|using|with|from)\b)"
    r"|\bbefore the (fix|change|refactor)\b|\bin the old\b"
    r"|\b(was|were)\s+(found|introduced|added|broken|fixed|changed|removed)\b"
    r"|\bregression (from|introduced)\b|\bas of\s+\d"
    r"|\b(19|20)\d{2}-\d{2}(-\d{2})?\b",
    re.I,
)


# Code that never reached `main`. Both arms name the author, not the program,
# as subject -- the property relied on; see docs/calibration.md.
UNBUILT = re.compile(
    r"\bwe\s+(?:also\s+|then\s+|later\s+|originally\s+)?"
    r"(?:removed|deleted|reverted|renamed|refactored|rewrote|un-?did"
    r"|took out|ripped out|backed out)\b"
    r"|\b(?:considered|tried|explored|experimented with|prototyped|attempted)\b"
    r"[^.]{0,80}\bbut\b[^.]{0,80}"
    r"\b(?:not needed|unnecessary|didn'?t help|did not help|didn'?t work"
    r"|did not work|no benefit|reverted|abandoned|backed out|dropped it"
    r"|removed it)\b",
    re.I,
)


# An edit with a consequence downstream is a premise: deleting it deletes the
# argument. `REASON` misses the pandas case ("have to", not "must").
UNBUILT_PREMISE = re.compile(
    r"\bhave to\b|\bhas to\b|\bneed(s)? to\b|\bwhich is why\b"
    r"|\bthat is why\b|\bhence\b|\bso (that|we|it|the)\b"
    r"|^\s*if\b|\bif we (have|had|hit|see)\b",
    re.I,
)


def unbuilt(body: str) -> bool:
    """Edit narration, unless the edit is the premise for the code below it.

    pandas' `generate_legacy_storage_files` is the one firing across both
    corpora, and it reads the other way: "if we have cases where we changed the
    signature (e.g. we renamed offset -> freq) ... then we have to conditionally
    execute" is why the script branches. Same deference as `benchmark`, for the
    same reason -- only the free-floating narration moves.
    """
    return bool(
        UNBUILT.search(body)
        and not REASON.search(body)
        and not UNBUILT_PREMISE.search(body)
    )


def verbose_limits(kind: str) -> tuple[int, int]:
    if kind == DOC:
        return DOC_VERBOSE_WORDS, DOC_VERBOSE_LINES
    return VERBOSE_WORDS, VERBOSE_LINES


def prose_lines(lines: tuple[str, ...]) -> int:
    """Lines carrying text.

    A block form's delimiters are not prose. Counting `/**` and `*/` would make
    a line limit mean "three lines of text" for a `#` run but "one" for a
    JSDoc block, and report a two-word comment as verbose.
    """
    return sum(1 for s in lines if s.strip())


# A parenthetical, example or unit states what the code does not, so it is not
# narration. Also rescues a verb's noun reading ("Cache duration (5 minutes)").
SPECIFICS = re.compile(r"\([^)]{4,}\)|\be\.g\.|\bi\.e\.|\u2192|\d")

REASON = re.compile(
    r"\bto (prevent|avoid|ensure|keep|stop|make sure|match|satisfy)\b"
    r"|\bso (that|the|it|we|a)\b|\bbecause\b|\bsince\b|\botherwise\b"
    r"|\brather than\b|\bwould\b|\bmust\b|\bcannot\b|\bcan't\b",
    re.I,
)

# A narrow grammatical form, not an open-ended verb tier: "If condition,
# perform action" labels the adjacent branch. Specifics stay outside it.
CONDITIONAL_ACTION = re.compile(
    r"^if\b[^.!?\n]{1,120},\s*"
    r"(add|append|build|calculate|call|clear|close|compute|continue|convert|"
    r"create|drop|filter|get|initialize|load|parse|raise|remove|return|reuse|"
    r"set|skip|sort|store|update|use|write)\b",
    re.I,
)


def conditional_narration(body: str) -> bool:
    return bool(
        len(RAW_WORD.findall(body)) <= MAX_WORDS
        and not REASON.search(body)
        and not SPECIFICS.search(body)
        and CONDITIONAL_ACTION.search(body)
    )


# Continuation markers, so block_context skips a block's own body lines
# (`*` in JS/Rust) rather than returning them as "the code".
CONT_PREFIX = ("#", "//", "*", "/*", '"""', "'''")


# A section a convention fixes: a parameter list is as long as the signature,
# so its length is not the author's. Rust's `Panics`/`Safety` are lint-demanded.
DOC_MARKER = re.compile(
    r"^[@\\](param|returns?|throws|example|deprecated|type|typedef|see|since"
    r"|template|internal|public|private|protected|readonly|default"
    r"|constructor|augments|extends|implements|fires|listens|yields|override)\b"
    r"|^#{1,6}\s+(Examples?|Panics|Safety|Errors|Arguments|Returns)\b",
    re.I,
)
# The free-text counterpart: nothing prescribes what goes in `Notes` or
# `@remarks`, which is exactly why an essay ends up there.
DOC_PROSE_MARKER = re.compile(r"^[@\\]remarks\b|^#{1,6}\s+Notes?\b", re.I)
NUMPY_SECTION = re.compile(
    r"^(Parameters|Returns|Yields|Raises|Examples?|Notes?|See Also|References"
    r"|Attributes|Warns|Other Parameters|Warnings)$"
)
NUMPY_PROSE_SECTIONS = frozenset({"notes", "note", "warnings"})
SECTION_RULE = re.compile(r"^[-=]{3,}$")


def numpy_section(lines: tuple[str, ...], i: int) -> str | None:
    """The lowercased numpydoc section header at `lines[i]`, or None.

    A header needs its underline, or "Notes" opening a sentence reads as one.
    """
    head = lines[i].strip()
    if (
        NUMPY_SECTION.match(head)
        and i + 1 < len(lines)
        and SECTION_RULE.match(lines[i + 1].strip())
    ):
        return head.lower()
    return None


def doc_prose(lines: tuple[str, ...]) -> tuple[str, ...]:
    """A doc block's chosen prose: its prologue plus every free-text section.

    A doc block's length is prescribed only where a convention prescribes it.
    Truncating at the first marker would exempt `Notes` and `@remarks` too,
    judging a 39-line docstring on the 22 words above a 19-line essay.

    A blanket `verbose` exemption would hide unbounded narrative, and the
    plain-comment ceiling would push bounded API behavior into artificial
    sections. Vetoing `@param` through NEVER would suppress the whole block,
    since NEVER is searched against the joined body.
    """
    out: list[str] = []
    keep = True
    i = 0
    while i < len(lines):
        section = numpy_section(lines, i)
        if section is not None:
            keep = section in NUMPY_PROSE_SECTIONS
            i += 2  # the header and its underline are neither prose nor a line
            continue
        line = lines[i].strip()
        prose = DOC_PROSE_MARKER.match(line)
        marker = prose or DOC_MARKER.match(line)
        if marker:
            keep = prose is not None
            # `@remarks The essay starts here` carries prose on the tag line.
            rest = line[marker.end() :].strip()
            if keep and rest:
                out.append(rest)
            i += 1
            continue
        if keep:
            out.append(lines[i])
        i += 1
    return tuple(out)


def classified_lines(b: Comment) -> tuple[str, ...]:
    """The lines the tiers judge: a doc block's prose, everything else whole.

    The verbose tier reads its span from here rather than from the block, or a
    short summary over a long `@param` list would be reported for the length of
    the list -- the prescribed part, which is not the author's choice.
    """
    return doc_prose(b.lines) if b.kind == DOC else b.lines


def _joined(lines: tuple[str, ...]) -> str:
    return " ".join(s.strip() for s in lines if s.strip()).strip()


def classified_body(b: Comment) -> str:
    """The text the tiers judge: a doc block's prologue, everything else whole."""
    return _joined(classified_lines(b))


def vetoed(body: str, lang: str = "") -> bool:
    """Whether one comment's text is exempt outright: empty, `#:` prefixed,
    NEVER, or its language's `veto`.

    The one copy of the per-comment exemption set: `exempt`, `group_blocks`,
    `density_sets` and `recalibrate.measure` all ask it, so a new `NEVER`
    alternative reaches every one of them at once.
    """
    if not body or body.startswith(":") or NEVER.search(body):
        return True
    veto = LANGUAGES[lang].veto if lang in LANGUAGES else None
    return bool(veto and veto.match(body))


def group_blocks(raw: list[Comment]) -> list[Comment]:
    """Merge a run of own-line comments into one block, keyed on its first line.

    `raw` is `extract` output, whose `inline` says which comments are own-line.

    A wrapped comment paragraph is one thought. Classifying each physical line
    separately judges continuation fragments out of context, so "next turn."
    reads as narrative slop when it is the tail of a real explanation.

    A pure delimiter line stays standalone: it is a structural marker, and
    folding it into the neighbouring prose would hide the very run of dashes
    that makes it reportable.

    Block forms carry `mergeable=False` and their own `lines`, so the cursor
    advances past their last physical line rather than their first.

    An exempt PLAIN line does not merge either. Grouping first and vetoing the
    joined body would let one comment silence its neighbours: `# TODO: revisit`
    above `# Fixed: use the cache` would produce no finding at all, and so would
    a URL, a `NOTE`, or a lint directive. Asking `vetoed` of each line keeps an
    exemption to the comment it matches, as `dockerfile_comments` does for a
    `# syntax=` directive above a build's opening explanation.

    PLAIN only, because there the merge is OUR choice: a run of `#` lines is a
    run of separate comments we decide to read as one paragraph. A `///` run is
    one comment in the language, and splitting it on a `# Safety` heading judges
    the prose under that heading out of the doc it belongs to, and would halve
    the Rust doc firing rate by nearly doubling the block count. Doc blocks get
    their section handling from `doc_prose` instead.
    """
    out: list[Comment] = []
    prev: Comment | None = None
    last = 0  # the block's LAST line, so a third line still merges
    for c in raw:
        if (
            out
            and prev is not None
            and c.mergeable
            and prev.mergeable
            and c.kind == prev.kind
            and not DELIM.match(c.body)
            and not DELIM.match(prev.body)
            and not (
                c.kind == PLAIN
                and (vetoed(c.body, c.lang) or vetoed(prev.body, prev.lang))
            )
            and not c.inline
            and not prev.inline
            and c.line == last + 1
            and c.col == prev.col
        ):
            out[-1] = replace(out[-1], lines=out[-1].lines + c.lines)
        else:
            out.append(c)
        last = c.line + c.span - 1
        prev = c
    return out


@dataclass(frozen=True)
class Subject:
    """A block as the tiers see it: its classified text and the code it annotates.

    Built once per block, so the doc-prose split is not redone per tier.
    """

    block: Comment
    lines: tuple[str, ...]
    body: str
    # The code sharing the comment's line, else the first line below it.
    target: str = ""


def subject(b: Comment, target: str = "") -> Subject:
    judged = classified_lines(b)
    return Subject(b, judged, _joined(judged), target)


def verbose(s: Subject) -> bool:
    word_limit, line_limit = verbose_limits(s.block.kind)
    return (
        len(RAW_WORD.findall(s.body)) > word_limit or prose_lines(s.lines) >= line_limit
    )


@dataclass(frozen=True)
class Tier:
    name: str
    test: Callable[[Subject], bool]
    # False where the signal is terseness, which is what a trailing annotation
    # IS: `timeout = 5  # seconds` would fire 133 times here as `bare-identifier`.
    inline_ok: bool = True


def _matches(rx: re.Pattern[str]) -> Callable[[Subject], bool]:
    return lambda s: bool(rx.search(s.body))


# The cascade, in order: the first tier to fire names the finding.
TIERS = (
    Tier(
        "restates-code",
        lambda s: bool(s.target) and restates(s.body, s.target),
        inline_ok=False,
    ),
    Tier("edit-narration", _matches(EDIT_NARRATION)),
    Tier("banner", _matches(BANNER)),
    Tier("section-label", _matches(SECTION_LABEL), inline_ok=False),
    Tier("bare-identifier", _matches(BARE_IDENTIFIER), inline_ok=False),
    Tier("emoji", _matches(EMOJI)),
    Tier("narrative", _matches(NARRATIVE), inline_ok=False),
    Tier("step-marker", _matches(STEP_MARKER)),
    Tier("meta", _matches(META)),
    Tier("code-reference", _matches(CODE_REF)),
    Tier("process-leak", _matches(PROCESS_LEAK), inline_ok=False),
    Tier("benchmark", lambda s: benchmark(s.body)),
    Tier("historical", _matches(HISTORICAL), inline_ok=False),
    Tier("unbuilt", lambda s: unbuilt(s.body), inline_ok=False),
    Tier(
        "conditional-narration",
        lambda s: conditional_narration(s.body),
        inline_ok=False,
    ),
    Tier("verbose", verbose),
)
# Every short comment the cascade let through. High recall and low precision,
# so only a deliberate sweep asks for it, and last so it never outranks a tier.
UNREVIEWED = Tier("unreviewed", lambda s: len(words(s.body)) <= MAX_WORDS)


def exempt(s: Subject) -> bool:
    """Whether a block is exempt before any tier runs. The one copy of that set."""
    lang = LANGUAGES.get(s.block.lang)
    return vetoed(s.body, s.block.lang) or bool(
        lang and lang.block_veto and lang.block_veto(s.lines)
    )


def fires(s: Subject, unreviewed: bool = False) -> str | None:
    """The first tier to fire on a block `exempt` let through, if any."""
    lang = LANGUAGES.get(s.block.lang)
    suppressed = lang.suppressed if lang else frozenset()
    for tier in (*TIERS, UNREVIEWED) if unreviewed else TIERS:
        if tier.name in suppressed or (s.block.inline and not tier.inline_ok):
            continue
        if tier.test(s):
            return tier.name
    return None


def block_reason(b: Comment, target: str = "", unreviewed: bool = False) -> str | None:
    """Which tier fires on this block, if any: `exempt`, then `fires`.

    For callers that judge a block outside `analyze`'s walk -- corpus
    generation, snapshot scoring and the firing-rate bands. It composes
    the same two functions the walk does, so a measurement and the hook agree
    about what fires.
    """
    s = subject(b, target)
    return None if exempt(s) else fires(s, unreviewed)


def block_context(lines: list[str], b: Comment, n: int) -> list[str]:
    """The first `n` non-blank code lines after the block's LAST line."""
    after = b.line + b.span - 1
    out = []
    for line in lines[after : after + n + 2]:
        text = line.strip()
        if text and not text.startswith(CONT_PREFIX):
            out.append(text)
            if len(out) == n:
                break
    return out


def density_sets(raw: list[Comment]) -> tuple[set[int], set[int]]:
    """(counted, neutral) line sets for the density ratio.

    The one copy of this construction: `analyze` and recalibrate.py both
    consume it, and a mirrored copy is how they drift apart.

    A line carrying both code and a trailing comment is a CODE line. Counting
    it as a comment would both inflate the numerator and remove a line from the
    denominator, so three annotated assignments would report "3 of 5 changed
    lines are comments (60%)" about a file with no comment-only line in it.
    """
    counted = {
        ln
        for c in raw
        if c.kind == PLAIN and not c.inline and not vetoed(c.body, c.lang)
        for ln in range(c.line, c.line + c.span)
    }
    neutral = {
        ln for c in raw if c.kind == DOC for ln in range(c.line, c.line + c.span)
    }
    return counted, neutral


def _density_counts(
    lines: list[str], scope, comment_lines: set[int], neutral_lines: set[int]
) -> tuple[int, int]:
    n_cmt = n_code = 0
    for i in scope:
        if not (1 <= i <= len(lines)) or not lines[i - 1].strip():
            continue
        if i in neutral_lines:
            continue
        if i in comment_lines:
            n_cmt += 1
        else:
            n_code += 1
    return n_cmt, n_code


def density_finding(
    src: str,
    path: str,
    added: set[int] | None,
    comment_lines: set[int],
    neutral_lines: set[int],
) -> Finding | None:
    """Flag a changed region that is more comment than reference practice.

    A per-comment tier cannot see narration-style commenting: every individual
    remark may be defensible while the density is not. Vetoed comments (lint
    directives and the other `NEVER` forms) are excluded from the numerator,
    or a file following such a convention would be flagged for it.

    `neutral_lines` leave the ratio entirely, on neither side. Counting an
    API-doc construct as code would make a heavily documented file read as
    comment-sparse; counting it as comment would flag it for being documented.
    """
    lang = language_of(path)
    ceiling = lang.density_max if lang else DENSITY_MAX
    lines = src.split("\n")
    scope = added if added is not None else set(range(1, len(lines) + 1))
    n_cmt, n_code = _density_counts(lines, scope, comment_lines, neutral_lines)
    if n_cmt < DENSITY_MIN_COMMENT_LINES:
        return None
    unit = "changed lines are comments"
    # No code in the change leaves no denominator -- 100% is the only value
    # the ratio could take, so measure the file instead.
    if n_code == 0 and added is not None:
        n_cmt, n_code = _density_counts(
            lines, range(1, len(lines) + 1), comment_lines, neutral_lines
        )
        unit = "lines in this file are comments"
    total = n_cmt + n_code
    ratio = n_cmt / total
    if ratio <= ceiling:
        return None
    return Finding(
        # The first CHANGED line either way: the finding points at the edit
        # that triggered the check, not at the top of a file it summarises.
        min(scope) if scope else 1,
        "density",
        f"{n_cmt} of {total} {unit} ({ratio:.0%}); "
        f"reference practice is under {ceiling:.0%}",
    )


Sweep = Callable[[list[Comment], list[str]], list[Finding]]


def analyze(
    src: str,
    path: str,
    added: set[int] | None = None,
    *,
    unreviewed: bool = False,
    sweep: Sweep | None = None,
) -> list[Finding]:
    """Every finding on `src`, read as the file at `path`, within the `added` lines.

    The pipeline without I/O: `scan` supplies `added` from git, `eval/score.py`
    a labelled case's lines, and None means the whole file. Density leads; the
    rest follow in line order.

    One walk applies scope and `exempt`, so no caller re-checks a part of the
    exemption set. `sweep` gets what the walk left -- in scope, not exempt, not
    fired on -- and returns its own findings; it is how `--llm` joins.
    """
    raw = extract(src, path)
    lines = src.split("\n")
    out: list[Finding] = []
    residue: list[Comment] = []
    for b in group_blocks(raw):
        # Scoping intersects the block's PHYSICAL range; the tiers judge the
        # classified lines. For a doc block those differ.
        if added is not None and added.isdisjoint(range(b.line, b.line + b.span)):
            continue
        before = lines[b.line - 1][: b.col].strip()
        context = block_context(lines, b, 4)
        s = subject(b, before or (context[0] if context else ""))
        if exempt(s):
            continue
        reason = fires(s, unreviewed)
        if reason:
            # Judged on the prologue, reported whole: the author needs to see
            # the comment they wrote, not the slice the tiers looked at.
            out.append(Finding(b.line, reason, b.body))
        else:
            residue.append(b)
    out = out[:MAX_REPORT]
    if sweep is not None and residue:
        out += sweep(residue, lines)
    counted, neutral = density_sets(raw)
    d = density_finding(src, path, added, counted, neutral)
    out.sort(key=lambda f: f.line)
    return ([d] if d else []) + out


#: With GIT_DIR set -- git exports it to every hook -- git takes the process cwd
#: for the work-tree root, so a path under a linked worktree resolves to nothing.
_GIT_LOCATION_VARS = ("GIT_DIR", "GIT_WORK_TREE")


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS}
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, timeout=10, env=env
    )
    return r.returncode, r.stdout.decode("utf-8", "replace")


def added_lines(path: str, ref: str = "HEAD") -> set[int] | None:
    # Absolute: `cwd` is the file's own directory, so a relative `tests/a.py`
    # would resolve there as `tests/tests/a.py` and read as "no git information".
    full = os.path.abspath(path)
    cwd = os.path.dirname(full) or "."
    if not os.path.isdir(cwd):
        return None
    try:
        rc, _ = _git(["ls-files", "--error-unmatch", "--", full], cwd)
        if rc != 0:
            return None
        rc, diff = _git(["diff", ref, "-U0", "--", full], cwd)
        if rc != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None

    out: set[int] = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        start, count = int(m.group(1)), int(m.group(2) or 1)
        out.update(range(start, start + count))
    return out


def repo_root(start: str) -> str | None:
    """The repository `start` is in, walking up for a `.git` entry.

    A file, not just a directory: linked worktrees and submodules spell it that
    way. Done here rather than by `rev-parse` because narrowing groups paths by
    repository, and a subprocess per directory defeats the point of narrowing.
    """
    d = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def keep_changed(paths: list[str], ref: str, since: bool) -> list[str]:
    """`paths` minus the ones git says are unchanged since `ref`.

    Grouped by repository, so a whole-repo invocation costs two `git` calls
    rather than the two per path `added_lines` would spend reaching the same
    answer. Paths git cannot answer for are kept -- except under an explicit
    `--since`, where an unanswerable ref is a comparison that did not happen.
    """
    sets: dict[str | None, set[str] | None] = {}
    out = []
    for path in paths:
        full = os.path.abspath(path)
        root = repo_root(os.path.dirname(full) or ".")
        if root not in sets:
            sets[root] = changed_files(ref, root) if root else None
        changed = sets[root]
        if changed is None:
            if not since:
                out.append(path)
        elif full in changed:
            out.append(path)
    return out


def changed_files(ref: str, cwd: str) -> set[str] | None:
    """Absolute paths that differ from `ref`, or None if git cannot say.

    Untracked files count: relative to `ref` they are additions, and a
    whole-file scan is what they get. Two calls answer this for the whole
    repository, where `added_lines` costs a `git diff` per path and prek can
    hand the gate every file in it -- 2900 here, about a minute of subprocesses.
    """
    try:
        rc, root = _git(["rev-parse", "--show-toplevel"], cwd)
        if rc != 0:
            return None
        top = root.strip()
        rc_diff, tracked = _git(["diff", "--name-only", ref], top)
        if rc_diff != 0:
            return None
        rc_new, new = _git(["ls-files", "--others", "--exclude-standard"], top)
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = tracked.split("\n") + (new if rc_new == 0 else "").split("\n")
    return {os.path.join(top, n) for n in names if n}


def warn(msg: str) -> None:
    print(f"comment-slop: {msg}", file=sys.stderr)


def _llm_sweep(hook: bool) -> Sweep | None:
    """Load only the companion beside this detector, and fail open if it breaks."""
    companion = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "comment_slop_llm.py"
    )
    if not os.path.isfile(companion):
        warn("--llm needs comment_slop_llm.py beside this module; skipped")
        return None

    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_comment_slop_llm_local", companion
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {companion}")
        llm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(llm)
        model_sweep = llm.sweep(llm.LLM_DEADLINE if hook else None)
        if not callable(model_sweep):
            raise TypeError("sweep() did not return a callable")
    except Exception as exc:
        warn(f"--llm companion unavailable ({exc}); skipped")
        return None

    def guarded(residue: list[Comment], lines: list[str]) -> list[Finding]:
        try:
            findings = model_sweep(residue, lines)
            if not isinstance(findings, list) or any(
                not isinstance(f, Finding) for f in findings
            ):
                raise TypeError("sweep() did not return findings")
            return findings
        except Exception as exc:
            warn(f"--llm companion failed ({exc}); deterministic findings only")
            return []

    return guarded


CONVENTION = (
    "Repo convention: comment the non-obvious why, not the what.\n"
    "Delete or rewrite these, or explain why each is load-bearing."
)


def scan(
    path: str,
    *,
    unreviewed: bool = False,
    sweep: Sweep | None = None,
    whole_file: bool = False,
    ref: str = "HEAD",
) -> list[Finding]:
    """`analyze` the file at `path`, scoped to the lines changed since `ref`.

    The I/O half of the pipeline: an unsupported, oversized or undecodable file
    reports nothing, and `whole_file` skips the git question entirely.
    """
    if language_of(path) is None:
        return []
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    added = None if whole_file else added_lines(path, ref)
    return analyze(src, path, added, unreviewed=unreviewed, sweep=sweep)


def _display(path: str) -> str:
    try:
        rel = os.path.relpath(path)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


def report(path: str, findings: list[Finding]) -> str:
    rows = [
        # density carries a whole sentence, not a quoted comment, so it is not
        # truncated to the comment-preview width
        f"  L{f.line:<4} {'[' + f.reason + ']':<20}"
        f"{f.text if f.reason == 'density' else f.text[:60]}"
        for f in findings
    ]
    return "\n".join([f"comment-slop: {_display(path)}", *rows])


# A sweep walks someone's checkout, where `node_modules` outnumbers source by
# orders of magnitude.

# `worktrees` is here because this repo sits under one, so a parent sweep would
# otherwise report every sibling's copy.

# `site-packages` prunes only BELOW a root, so `recalibrate.py` can still walk
# a reference package installed there.
SWEEP_SKIP_DIRS = frozenset(
    {
        "node_modules",
        "target",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        "coverage",
        "vendor",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        "worktrees",
        ".nox",
    }
)


def walk_source(root: str) -> list[str]:
    """Every file under `root`, pruning `SWEEP_SKIP_DIRS` as it descends.

    Pruning, not filtering the results: `rglob` per pattern still walks into
    every `node_modules` and `target` it finds and only discards the paths
    afterwards. Over a tree of checkouts that is the difference between 40s and
    3s, which is why every corpus walker -- tests/, eval/ and `recalibrate.py`
    -- shares this rather than keeping its own list.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SWEEP_SKIP_DIRS]
        out += [os.path.join(dirpath, f) for f in sorted(filenames)]
    return out


def expand(paths: list[str]) -> list[str]:
    """Files named directly, plus every supported file a pattern or a directory
    names.

    Left unexpanded, a quoted `'frontend/src/**/*.ts'` would reach
    `os.path.getsize` as a literal filename and scan nothing, silently. Quoting
    it is not optional either -- whether a bare `**` recurses depends on the
    shell -- so expanding it here is the only place it can happen.
    """
    import glob

    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen and language_of(path) is not None:
            seen.add(path)
            out.append(path)

    for spec in paths:
        if os.path.isfile(spec):
            add(spec)
        elif os.path.isdir(spec):
            for path in walk_source(spec):
                add(path)
        else:
            for hit in sorted(glob.glob(spec, recursive=True)):
                if os.path.isfile(hit):
                    add(hit)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Report comment slop on changed lines.")
    ap.add_argument(
        "--hook", action="store_true", help="read a PostToolUse JSON payload on stdin"
    )
    ap.add_argument(
        "--include-unreviewed",
        action="store_true",
        help="also report every short unmatched comment "
        "(high recall, low precision \u2014 manual sweeps only)",
    )
    ap.add_argument(
        "--llm",
        action="store_true",
        help="also ask the local model which comments are narration "
        "(a deliberate sweep; too slow for the hook)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        dest="whole_file",
        help="report on every line, not only those changed since "
        "HEAD. Required to sweep already-committed code",
    )
    ap.add_argument(
        "--since",
        metavar="REF",
        help="compare against REF instead of HEAD, and scan only the files REF "
        "changed. The gate's CI mode: prek there runs over a clean tree, where "
        "a HEAD comparison reports nothing at all",
    )
    ap.add_argument(
        "paths",
        nargs="*",
        help="files, directories, or glob patterns (`**` included; "
        "quote them so this expands them, not the shell)",
    )
    a = ap.parse_args(argv)

    ref = a.since or "HEAD"
    if a.hook:
        try:
            payload = json.load(sys.stdin)
            paths = [payload["tool_input"]["file_path"]]
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            return 0
    else:
        paths = expand(a.paths)
        # `--all` deliberately ignores the diff, so nothing may be narrowed away.
        if not a.whole_file:
            paths = keep_changed(paths, ref, bool(a.since))
    if not paths:
        return 0

    sweep = _llm_sweep(a.hook) if a.llm else None

    blocks = []
    for p in paths:
        try:
            findings = scan(
                p,
                unreviewed=a.include_unreviewed,
                sweep=sweep,
                whole_file=a.whole_file,
                ref=ref,
            )
        except Exception:
            continue
        if findings:
            blocks.append(report(p, findings))
    if not blocks:
        return 0

    print("\n\n".join(blocks), file=sys.stderr)
    print(CONVENTION, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # Run as a script this module is `__main__`, and `comment_slop_llm` would
    # import a second copy of it under its real name.
    sys.modules.setdefault("comment_slop", sys.modules[__name__])
    sys.exit(main())
