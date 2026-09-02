#!/usr/bin/env python3
"""Report agent-authored comment slop on lines changed since HEAD."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, replace
import io
import json
import os
import re
import subprocess
import sys
import time
import tokenize

# Imported at use, not here: sweep-only paths, and this module reloads on every
# edit. `urllib.request` alone is 17ms of an edit's ~85ms.

MAX_WORDS = 12
MAX_REPORT = 20
MAX_BYTES = 500_000

# Of a function's lines: numpy p95 27.8%, pandas 25.9%, scipy 33.3%. NOT
# lowered to the tightest corpus -- see docs/calibration.md.
DENSITY_MAX = 0.30
# Floor on the COMMENT side, not the region size. A region-size floor exempted
# the worst case there is -- a 5-line comment block on a 9-line change.
DENSITY_MIN_COMMENT_LINES = 3

PY_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
# TypeScript forbids JSX in `.ts` so that `<Foo>x` can stay a type assertion.
# Babel allows it in `.js`, so only these three opt out. See `js_comments`.
TS_ONLY_EXT = {".ts", ".mts", ".cts"}
JSX_EXT = JS_EXT - TS_ONLY_EXT
YAML_EXT = {".yml", ".yaml"}
RUST_EXT = {".rs"}
TOML_EXT = {".toml"}
# `web.dockerfile` is the suffixed spelling; the bare `Dockerfile` name is
# resolved by `lang_key`, which maps it onto these same keys.
DOCKER_EXT = {".dockerfile", ".containerfile"}
DOCKER_STEMS = {"dockerfile", "containerfile"}
# Extensions only: `lang_key` is given a path, not the source, so a shebang is
# unreadable to it. No extensionless scripts here to pay for that yet.
SH_EXT = {".sh", ".bash", ".zsh", ".ksh"}

PLAIN, DOC = "plain", "doc"

TOML = "toml"
YAML = "yaml"
DOCKER = "dockerfile"
SHELL = "shell"

# Per language: DENSITY_MAX is a percentile of a Python FUNCTION, a script's
# scope is its file. Shell p95 38.5% (vcpkg); basis in docs/calibration.md.
DENSITY_CEILINGS = dict.fromkeys(SH_EXT, 0.40)


@dataclass(frozen=True)
class Comment:
    """One comment as its extractor found it.

    `lines` holds one entry per PHYSICAL line with the comment markers
    stripped, so a delimiter-only line is an empty entry. `span` and `body`
    derive from it and therefore cannot disagree with it -- the earlier shape
    stored both alongside the text, and the three copies of the classification
    logic drifted apart on exactly that.

    `mergeable` is False for block forms so they never absorb a neighbouring
    line comment.
    """

    line: int
    col: int
    lines: tuple[str, ...]
    kind: str = PLAIN
    mergeable: bool = True
    # Set only where a tier is scoped to one language. Travels with the comment
    # so production and every measurement path agree on what fires.
    lang: str = ""
    # Shares its line with code. Set once in `extract`, like `lang`: three call
    # sites judged it separately and disagreed.
    inline: bool = False

    @property
    def span(self) -> int:
        return len(self.lines)

    @property
    def body(self) -> str:
        return " ".join(s.strip() for s in self.lines if s.strip()).strip()


@dataclass(frozen=True)
class Block(Comment):
    """What _group_blocks returns. Same shape; merging concatenates `lines`."""


def _py_line_comments(src: str) -> list[Comment]:
    """Every `#` comment as a plain one, `#:` included.

    `#:` is the Sphinx/Pydantic attribute-doc marker, and it is stripped like
    any other comment marker rather than earning a kind. It had one, with a
    doc-sized budget and density neutrality, and that combination is what let a
    58-word operational narrative on a one-line constant through: nothing a
    reader gains from `#:` makes the prose after it less of an ordinary comment.

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
    """
    try:
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

    JSX children are literal text, not code: a shell snippet in a `<Box
    component="pre">` had its `/*` read as a comment, inventing a 56-line one.

    The frame stack is the mechanism: `{` in a tag or text pushes CODE back on.

    `jsx` is False for `.ts`/`.mts`/`.cts`, where TypeScript forbids JSX so
    `<Foo>x` stays a type assertion instead of eating the file as text.

    Unbalanced JSX leaves a TEXT frame open, silencing the rest -- the safe
    direction, as for an unterminated heredoc.
    """
    out: list[Comment] = []
    n = len(src)
    i, line, line_start = 0, 1, 0
    prev_sig = ""
    # [kind, brace_depth]; only CODE frames use the depth.
    frames: list[list] = [[_CODE, 0]]

    def advance_over(end: int) -> None:
        nonlocal i, line, line_start
        nl = src.rfind("\n", i, end)
        if nl != -1:
            line += src.count("\n", i, end)
            line_start = nl + 1
        i = end

    while i < n:
        c = src[i]
        kind = frames[-1][0]

        if c == "\n":
            line, i = line + 1, i + 1
            line_start = i
            continue

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
                advance_over(end + 2)
                frames.append([_CODE, 0])
                continue
            advance_over(end)
            frames.pop()
            prev_sig = "`"
            continue

        if kind == _TEXT:
            if c == "<" and i + 1 < n:
                if src[i + 1] == "/":
                    end = src.find(">", i)
                    advance_over(n if end == -1 else end + 1)
                    frames.pop()
                    prev_sig = ")"  # an element is a value
                    continue
                # A tag name or `<>`; anything else is text, since prose says
                # `5 < 6`. As a tag it pushed a TEXT frame `</Box>` can't pop.
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
                out.append(Comment(line, i - line_start, (src[i + 2 : end].strip(),)))
            advance_over(end)
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
                    line,
                    i - line_start,
                    _strip_stars(inner),
                    DOC if is_doc else PLAIN,
                    mergeable=False,
                )
            )
            advance_over(end)
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
                advance_over(_js_string_end(src, i))
                continue
            i += 1
            continue

        # Everything below is reached only in a CODE frame, so the string,
        # brace, element and regex cases need no kind test of their own.
        if c in "'\"`":
            end = _js_string_end(src, i)
            if c == "`" and end < n and src[end : end + 2] == "${":
                advance_over(end + 2)
                frames.append([_TPL, 0])
                frames.append([_CODE, 0])
                continue
            advance_over(end)
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
            advance_over(_js_regex_end(src, i))
            prev_sig = "/"
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return out


def ts_comments(src: str) -> list[Comment]:
    """`.ts`/`.mts`/`.cts`, where TypeScript forbids JSX. See `js_comments`."""
    return js_comments(src, jsx=False)


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
            out.append(Comment(lineno, cut, (line[cut + 1 :].strip(),), lang=YAML))
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
    n = len(src)
    i, line, line_start = 0, 1, 0

    def advance_over(end: int) -> None:
        nonlocal i, line, line_start
        nl = src.rfind("\n", i, end)
        if nl != -1:
            line += src.count("\n", i, end)
            line_start = nl + 1
        i = end

    while i < n:
        c = src[i]
        if c == "\n":
            line, i = line + 1, i + 1
            line_start = i
            continue
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
                advance_over(min(j, n))
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
            advance_over(min(j, n))
            continue
        if c == "#":
            end = src.find("\n", i)
            end = n if end == -1 else end
            out.append(
                Comment(line, i - line_start, (src[i + 1 : end].strip(),), lang=TOML)
            )
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
            # `## Section` is the common banner form; leaving the second marker
            # in the body hid it from the tiers that read the first word.
            body = head.lstrip("#").strip()
            # A directive is not prose, and a build's opening explanation sits
            # right under one. Merged, the veto on the directive silenced it.
            out.append(
                Comment(
                    lineno,
                    len(line) - len(head),
                    (body,),
                    mergeable=not DIRECTIVE.match(body),
                    lang=DOCKER,
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
    n = len(src)
    i, line, line_start = 0, 1, 0
    pending: list[str] = []  # heredoc delimiters awaiting their terminator

    def advance_over(end: int) -> None:
        nonlocal i, line, line_start
        nl = src.rfind("\n", i, end)
        if nl != -1:
            line += src.count("\n", i, end)
            line_start = nl + 1
        i = end

    while i < n:
        c = src[i]
        if c == "\n":
            line, i = line + 1, i + 1
            line_start = i
            while pending and i < n:
                end = src.find("\n", i)
                end = n if end == -1 else end
                # `<<-` permits a tab-indented terminator, so compare stripped.
                if src[i:end].strip() == pending[0]:
                    pending.pop(0)
                i = end + 1 if end < n else n
                line += 1
                line_start = i
            continue
        if c == "\\":
            # Through advance_over, not `i += 2`: the escaped char is often a
            # continued command's newline, uncounted then by every later line.
            advance_over(min(i + 2, n))
            continue
        if c == "'":
            end = src.find("'", i + 1)
            advance_over(n if end == -1 else end + 1)
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
            advance_over(min(j, n))
            continue
        if src[i : i + 2] == "<<":
            m = SH_HEREDOC.match(src, i)
            if m:
                pending.append(m.group(2))
                i = m.end()
                continue
            i += 2
            continue
        if c == "#" and (i == line_start or src[i - 1] in SH_WORD_BREAK):
            end = src.find("\n", i)
            end = n if end == -1 else end
            # `## Section` is a common banner form, and the second marker
            # hides it from the tiers that read the first word.
            out.append(
                Comment(
                    line, i - line_start, (src[i:end].lstrip("#").strip(),), lang=SHELL
                )
            )
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
    then consumes the rest of the file; and `col` is derived from the last
    newline, so a construct ending mid-line leaves the cursor mid-line.

    Per rustc, `////` and `/**/` are ordinary comments, not doc forms.
    """
    out: list[Comment] = []
    n = len(src)
    i, line, line_start = 0, 1, 0

    def advance_over(end: int) -> None:
        nonlocal i, line, line_start
        nl = src.rfind("\n", i, end)
        if nl != -1:
            line += src.count("\n", i, end)
            line_start = nl + 1
        i = end

    while i < n:
        c = src[i]
        if c == "\n":
            line, i = line + 1, i + 1
            line_start = i
            continue
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
                    advance_over(n if end == -1 else end + len(close))
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
            advance_over(j)
            continue
        if c == "'":
            if i + 1 < n and src[i + 1] == "\\":
                j = i + 2
                while j < n and src[j] not in "'\n":
                    j += 1
                advance_over(min(j + 1, n))
                continue
            if i + 2 < n and src[i + 2] == "'":
                advance_over(i + 3)
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
                        line,
                        i - line_start,
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
                start_line, start_col = line, i - line_start
                advance_over(j)
                out.append(
                    Comment(
                        start_line,
                        start_col,
                        _strip_stars(inner),
                        DOC if is_doc else PLAIN,
                        mergeable=False,
                    )
                )
                continue
        i += 1
    return out


EXTRACTORS = {
    **dict.fromkeys(PY_EXT, py_comments),
    **dict.fromkeys(JSX_EXT, js_comments),
    **dict.fromkeys(TS_ONLY_EXT, ts_comments),
    **dict.fromkeys(YAML_EXT, yaml_comments),
    **dict.fromkeys(RUST_EXT, rust_comments),
    **dict.fromkeys(TOML_EXT, toml_comments),
    **dict.fromkeys(DOCKER_EXT, dockerfile_comments),
    **dict.fromkeys(SH_EXT, shell_comments),
}
SUPPORTED = set(EXTRACTORS)


def lang_key(path: str) -> str:
    """The `EXTRACTORS` key for `path`.

    A Dockerfile names its language instead of suffixing it: `Dockerfile`,
    `Dockerfile.prod` and `Containerfile` all have a suffix `os.path.splitext`
    either cannot see or reads as the variant. A real supported extension still
    wins, so `Dockerfile.py` stays Python rather than losing every docstring.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in SUPPORTED:
        return ext
    stem = os.path.basename(path).lower().split(".", 1)[0]
    return "." + stem if stem in DOCKER_STEMS else ext


def shares_line_with_code(lines: list[str], lineno: int, col: int) -> bool:
    """Whether code precedes a comment starting at (`lineno`, `col`).

    `Comment.inline` is this answer, stamped once in `extract`. The rule stays
    a function because `_group_blocks` is also called with raw lexer output,
    where `inline` is not yet set, and two spellings of "own line" are how the
    merge rule and the density ratio would come to disagree.
    """
    return lineno <= len(lines) and bool(lines[lineno - 1][:col].strip())


def extract(src: str, path: str) -> list[Comment]:
    """Comments for `path`, or [] if unsupported or the lexer failed.

    A defect in one lexer degrades that file to "no findings" rather than
    propagating: the hook must never break the edit loop.

    `inline` and the shebang's unmergeability are stamped on here rather than in
    each lexer, so every consumer -- the tiers, the density ratio, `score.py`,
    `recalibrate.py` -- reads one answer to both questions.
    """
    fn = EXTRACTORS.get(lang_key(path))
    if fn is None:
        return []
    try:
        raw = fn(src)
    except Exception:
        return []
    lines = src.split("\n")
    shebang = src.startswith("#!")
    out = []
    for c in raw:
        if shares_line_with_code(lines, c.line, c.col):
            c = replace(c, inline=True)
        # A shebang is not prose, and `find_findings` skips it. Left mergeable
        # it absorbed line 2 and the skip silenced a real comment there.
        if shebang and c.line == 1 and c.col == 0:
            c = replace(c, mergeable=False)
        out.append(c)
    return out


# NEVER is case-insensitive except the issue key: folding case read `us-east-1`
# as one, and `(?<![\w-])` stops `US-EAST-1` matching through its `EAST-1` tail.
NEVER = re.compile(
    r"""
    noqa | type:\s*ignore | pylint: | pyright: | mypy: | ruff: | flake8:
  | eslint- | prettier-ignore | biome-ignore | @ts- | c8\s+ignore | istanbul\s+ignore
  | pragma | fmt:\s*(on|off) | yapf: | isort: | shellcheck
  # Uppercase only, like the ticket-ID alternative below. These are ordinary
  # English words too, and case-insensitively `a real lexer bug sat here` read
  # as a `BUG:` marker, and a veto is a miss no tier can report.
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

PATTERNS = [
    # Closed vocabulary: the ways to say "I just edited this" enumerate.
    # Measured 4/6 before `rework`/`tweak`. Checked before REASON.
    #
    # Completed-edit forms only: an imperative is how a docstring summary reads,
    # so `updated?`/`drop(ped)?` cost 2 of the corpus's 6 false positives.
    (
        "edit-narration",
        re.compile(
            r"^\(?\s*(added|adding|updated|fixe[ds]|changed|modified|"
            r"removed|renamed|refactor(ed|ing)|rework(ed|ing)|tweak(ed|ing)|"
            r"adjust(ed|ing)|simplified|clean(ed) up|dropped|switched|"
            r"moved|inlined|extracted|renaming|reverted|"
            r"was|previously|instead of)\b"
            r"|^\(?\s*(fix|new|add|update|change|remove|drop|move|split|"
            r"refactor|rework|revert)\s*:",
            re.I,
        ),
    ),
    (
        "banner",
        re.compile(
            r"^[=\-*~_#\s]{3,}$"  # pure divider
            # Labels carry hyphens and dots, which a \w-only class missed. 120
            # bounds the regex without reading display width as semantics.
            r"|^[=\-*~_#]{3,}\s*\w[\w\s.,'/-]{0,120}?\s*[=\-*~_#]{3,}\s*$"  # == LABEL ==
        ),
    ),
    (
        "section-label",
        re.compile(
            r"^(helpers?|utils?|utilities|constants?|imports?|types?|main|setup|"
            r"config(uration)?|exports?|public api|private|globals?|state|"
            r"variables?|functions?|classes?|interfaces?)\s*[:=\-]*$",
            re.I,
        ),
    ),
    # A lone identifier labels the thing below and adds nothing. A dot needs
    # identifier characters after it, or "Workspaces." reads as one.
    ("bare-identifier", re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*$")),
    ("emoji", re.compile("[\u2600-\u27bf\U0001f300-\U0001faff\u2705\u274c\u26a0]")),
    (
        "narrative",
        re.compile(
            r"^(now|first(ly)?|next|then|finally|lastly)\b|"
            r"^(let'?s|i'?ll|you can)\b",
            re.I,
        ),
    ),
    (
        "step-marker",
        re.compile(r"^(step|test|case|part|phase)\s*\d+\b|^\d+[.)]\s+\w", re.I),
    ),
    (
        "meta",
        re.compile(
            r"\b(for (clarity|readability|safety|completeness|consistency)|"
            r"as requested|to be safe|just in case|self[- ]explanatory|"
            r"as (you )?(can see|mentioned)|obviously|simply)\b",
            re.I,
        ),
    ),
]


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


# Raw words; numpy p98=38, pandas p98=35, median 7 here too. Lowered from 35 on
# 116 labels: kept comments top out at 30, and 35 caught 6 of 46 misses.
VERBOSE_WORDS = 30
# Lowered from 5 on 116 human labels: no comment the author kept ran past two
# lines, while 15 of 46 labelled misses were 3 lines or more.
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
# deletes the reason. Both of this tier's hits on this file were of that kind.
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


def verbose_span(span: int, kind: str = PLAIN) -> bool:
    return span >= verbose_limits(kind)[1]


def prose_lines(lines: tuple[str, ...]) -> int:
    """Lines carrying text.

    A block form's delimiters are not prose. Counting `/**` and `*/` made
    a line limit mean "three lines of text" for a `#` run but "one" for a
    JSDoc block, which reported a two-word comment as verbose.
    """
    return sum(1 for s in lines if s.strip())


def _group_blocks(raw: list[Comment], lines: list[str]) -> list[Block]:
    """Merge a run of own-line comments into one block, keyed on its first line.

    A wrapped comment paragraph is one thought. Classifying each physical line
    separately judges continuation fragments out of context, so "next turn."
    reads as narrative slop when it is the tail of a real explanation.

    A pure delimiter line stays standalone: it is a structural marker, and
    folding it into the neighbouring prose would hide the very run of dashes
    that makes it reportable.

    Block forms carry `mergeable=False` and their own `lines`, so the cursor
    advances past their last physical line rather than their first.

    An exempt PLAIN line does not merge either. Grouping first and vetoing the
    joined body let one comment silence its neighbours: `# TODO: revisit` above
    `# Fixed: use the cache` produced no finding at all, and so did a URL, a
    `NOTE`, or a lint directive. `dockerfile_comments` already kept a
    `# syntax=` directive out of the build's opening explanation for the same
    reason; `_self_vetoed` generalises it to every exemption.

    PLAIN only, because there the merge is OUR choice: a run of `#` lines is a
    run of separate comments we decide to read as one paragraph. A `///` run is
    one comment in the language, and splitting it on a `# Safety` heading judges
    the prose under that heading out of the doc it belongs to -- it took the Rust
    doc firing rate from 9.6% to 4.5% by nearly doubling the block count. Doc
    blocks get their section handling from `doc_prose` instead.
    """

    def own_line(lineno: int, col: int) -> bool:
        return not shares_line_with_code(lines, lineno, col)

    out: list[Block] = []
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
            and not (c.kind == PLAIN and (_self_vetoed(c) or _self_vetoed(prev)))
            and own_line(c.line, c.col)
            and own_line(prev.line, prev.col)
            and c.line == last + 1
            and c.col == prev.col
        ):
            # `replace`/`vars` rather than a positional copy: `lang` and
            # `inline` are adjacent, and a transposed str/bool would not raise.
            out[-1] = replace(out[-1], lines=out[-1].lines + c.lines)
        else:
            out.append(Block(**vars(c)))
        last = c.line + c.span - 1
        prev = c
    return out


def vetoed(body: str, lang: str = "") -> bool:
    """Whether a body is exempt outright: empty, `#:` prefixed, NEVER, or
    language-scoped.

    The one copy of the exemption set. `block_reason`, `density_sets`,
    `_self_vetoed` and `recalibrate.measure` each spelled it out, and the next
    entry the README invites into `NEVER` would have had to be right in four
    places.
    """
    return (
        not body
        or body.startswith(":")
        or bool(NEVER.search(body))
        or lang_vetoed(lang, body)
    )


def _self_vetoed(c: Comment) -> bool:
    """Whether a comment is exempt on its OWN text, before any merging.

    The same exemptions `block_reason` applies to a block, asked one physical
    comment at a time so they cannot spread to a neighbour.
    """
    return vetoed(c.body, c.lang)


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


# Config files disable a setting by commenting it out, not deleting it, so a
# run of `key =` lines under one explanatory line reads as long prose.
CONFIG_LINE = re.compile(
    r"""^\s*(\[\[?[A-Za-z0-9_."'-]+\]\]?\s*$   # [table], [[array of tables]]
        |["'A-Za-z0-9_.-]+\s*=                 # key =
        |["'][^"']*["']\s*,?$                  # a lone list element
        |[\]}],?\s*$)""",
    re.X,
)

# `bare-identifier` reads a lone name as adding nothing over the name below it;
# in a lint-ignore list those are opaque codes, so it was wrong 15 times of 15.
SUPPRESSED_TIERS = {TOML: frozenset({"bare-identifier"})}

# Tiers whose signal is terseness, which is what a trailing annotation IS:
# `timeout = 5  # seconds` fired 133 times here as `bare-identifier`.
INLINE_SUPPRESSED = frozenset(
    {
        "bare-identifier",
        "restates-code",
        "section-label",
        "narrative",
        "conditional-narration",
        "historical",
        "unbuilt",
        "process-leak",
    }
)


def commented_out_config(lines: tuple[str, ...]) -> bool:
    """Whether a block is mostly disabled configuration rather than prose."""
    text = [s for s in lines if s.strip()]
    if not text:
        return False
    return sum(1 for s in text if CONFIG_LINE.match(s)) >= max(1, len(text) // 2)


# `uses: owner/repo@<sha>  # v6.2.0` -- the tag survives nowhere else on the
# line, so the annotation is the convention rather than a label for it.
SHA_PIN = re.compile(
    r"^(?:pin(?:ned)?\s+(?:to\s+)?)?"
    r"(?:v\d+(?:\.\d+){0,3}|\d+(?:\.\d+){1,3})"
    r"(?:[.+-][A-Za-z0-9.+-]+)?"
    r"(?:\s*[(,-]?\s*sha[- ]?pin(?:ned)?\)?)?$",
    re.I,
)

# Unlike SUPPRESSED_TIERS these also leave the density numerator, or a fully
# pinned workflow is flagged for pinning everything.
LANG_VETOES = {YAML: SHA_PIN, DOCKER: DIRECTIVE}


def lang_vetoed(lang: str, body: str) -> bool:
    rx = LANG_VETOES.get(lang)
    return bool(rx and rx.match(body))


LATE_TIERS = [
    ("code-reference", lambda b: bool(CODE_REF.search(b))),
    ("process-leak", lambda b: bool(PROCESS_LEAK.search(b))),
    ("benchmark", benchmark),
    ("historical", lambda b: bool(HISTORICAL.search(b))),
    ("unbuilt", unbuilt),
    ("conditional-narration", conditional_narration),
]


def model_eligible(body: str) -> bool:
    """Whether a comment is worth spending a model call on.

    REASON and SPECIFICS are kept from the retired verb-list tier: they were
    doing the discriminative work, and they cheaply keep obvious keepers out of
    the batch. There is no verb list -- naming which verbs count was
    unmaintainable, and a POS tagger cannot help (taggers read "Configure" and
    "Loop" as nouns in comment-initial position, and imperative mood does not
    separate the classes anyway: "Guard against sentinel rows the upstream feed
    emits" is imperative and worth keeping).
    """
    return not (REASON.search(body) or SPECIFICS.search(body))


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

    A doc block's length is prescribed only where a convention prescribes it,
    and truncating at the first marker read that as "the prologue is all the
    author chose". It exempted `Notes` and `@remarks` too, so a 39-line
    docstring was judged on the 22 words above a 19-line essay.

    A blanket `verbose` exemption hid unbounded narrative, while reusing the
    plain-comment ceiling pushed bounded API behavior into artificial sections.
    Vetoing `@param` through NEVER also suppressed the whole block, because
    NEVER is searched against the joined body.
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


def classified_lines(b: Block) -> tuple[str, ...]:
    """The lines the tiers judge: a doc block's prose, everything else whole.

    The verbose tier reads its span from here rather than from the block, or a
    short summary over a long `@param` list would be reported for the length of
    the list -- the prescribed part, which is not the author's choice.
    """
    return doc_prose(b.lines) if b.kind == DOC else b.lines


def classified_body(b: Block) -> str:
    """The text the tiers judge: a doc block's prologue, everything else whole."""
    return " ".join(s.strip() for s in classified_lines(b) if s.strip()).strip()


def block_reason(b: Block, target: str = "") -> str | None:
    """Which tier fires on this block, if any. The one copy of this logic.

    Calibration, corpus generation and production all called their own
    near-copy; two omitted `restates-code` and one also omitted the span tier,
    so a measurement and the hook could disagree about what fires.
    """
    judged = classified_lines(b)
    body = " ".join(s.strip() for s in judged if s.strip()).strip()
    if vetoed(body, b.lang):
        return None
    if b.lang == TOML and commented_out_config(judged):
        return None
    suppressed = SUPPRESSED_TIERS.get(b.lang, frozenset())
    if b.inline:
        suppressed = suppressed | INLINE_SUPPRESSED
    if target and "restates-code" not in suppressed and restates(body, target):
        return "restates-code"
    for name, rx in PATTERNS:
        if name not in suppressed and rx.search(body):
            return name
    for name, test in LATE_TIERS:
        if name not in suppressed and test(body):
            return name
    word_limit, _ = verbose_limits(b.kind)
    if len(RAW_WORD.findall(body)) > word_limit or verbose_span(
        prose_lines(judged), b.kind
    ):
        return "verbose"
    return None


def block_fires(b: Block) -> bool:
    """Whether any tier fires, ignoring code context. For firing rates only."""
    return block_reason(b) is not None


def block_context(lines: list[str], b: Block, n: int) -> list[str]:
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


def find_findings(
    src: str,
    path: str,
    added: set[int] | None,
    include_unreviewed: bool = False,
    raw: list[Comment] | None = None,
) -> list[Finding]:
    if raw is None:
        raw = extract(src, path)
    lines = src.split("\n")
    grouped = _group_blocks(raw, lines)
    out = []
    for b in grouped:
        lineno, col = b.line, b.col
        # Scoping intersects the block's PHYSICAL range; the tiers judge the
        # classified lines. For a doc block those differ.
        if added is not None and not (added & set(range(lineno, lineno + b.span))):
            continue
        body = classified_body(b)
        # `#!` only. Rust's `//!` is inner documentation; skipping it lost a
        # crate's opening block. JS `//!` never reaches here.
        if lineno == 1 and src.startswith("#!"):
            continue
        # Repeated inside block_reason, but the `unreviewed` tier below would
        # otherwise report an empty or vetoed body for having few words.
        if not body or body.startswith(":") or NEVER.search(body):
            continue
        if lineno > len(lines):
            continue

        before = lines[lineno - 1][:col].strip()
        context = block_context(lines, b, 4)
        target = before or (context[0] if context else "")
        reason = block_reason(b, target)
        if reason is None and include_unreviewed and len(words(body)) <= MAX_WORDS:
            reason = "unreviewed"
        if reason:
            # Judged on the prologue, reported whole: the author needs to see
            # the comment they wrote, not the slice the tiers looked at.
            out.append(Finding(lineno, reason, b.body))
    return out


def density_sets(raw: list[Comment]) -> tuple[set[int], set[int]]:
    """(counted, neutral) line sets for the density ratio.

    The one copy of this construction: scan(), eval/score.py and
    recalibrate.py all consume it, and a mirrored copy is how they drift apart.

    A line carrying both code and a trailing comment is a CODE line. Counting
    it as a comment both inflated the numerator and removed a line from the
    denominator, so three annotated assignments reported "3 of 5 changed lines
    are comments (60%)" about a file with no comment-only line in it.
    """
    counted = {
        ln
        for c in raw
        if c.kind == PLAIN and not c.inline and not _self_vetoed(c)
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
    ceiling = DENSITY_CEILINGS.get(lang_key(path), DENSITY_MAX)
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


def _git(args: list[str], cwd: str) -> tuple[int, str]:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=10)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def added_lines(path: str, ref: str = "HEAD") -> set[int] | None:
    # Absolute: `cwd` is the file's own directory, so a relative `tests/a.py`
    # resolved there as `tests/tests/a.py` and read as "no git information".
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


LLM_API = os.environ.get(
    "COMMENT_SLOP_API", "http://127.0.0.1:8501/v1/chat/completions"
)
LLM_MODEL = os.environ.get("COMMENT_SLOP_MODEL", "local")
LLM_TIMEOUT = int(os.environ.get("COMMENT_SLOP_TIMEOUT", "120"))
LLM_BATCH = 12
# Wall-clock budget for the whole sweep. A batch cap bounds the number of
# calls, not the time they take: 3 batches x LLM_TIMEOUT is a six-minute stall.
LLM_DEADLINE = 20

# A/B tested on a 75-case corpus: naming the target classes took recall 92% to
# 100%, but keep-precision to 48%.

# The model over-generates whichever label it is asked for, so asking which to
# KEEP rather than DELETE is what reaches 99%.

# Thinking stays off -- ~70s a batch against ~1.2s, for no gain.
LLM_CONTEXT_LINES = 4  # 1 line scored 97%, 4 lines 99%, at the same latency
LLM_PROMPT = """You review code comments and report which ones should stay.

A comment should NOT stay if:
1. It restates what the code plainly does ("Loop over every row" above a `for`).
2. It narrates the edit that was made ("Fixed:", "Reworked to", "Tweaked").
3. It labels an obvious section ("Helpers", "Utilities", "===== TOTALS =====").
4. It leaks process or planning ("Task 3:", "Step 2:", "in this PR").
5. It records history that belongs in git ("previously asserted", "used to accept").
6. It points at a line number that will rot ("resource_plan.py:412", "lines 46-49").
7. It is a long explanation where a short one would do.

A comment SHOULD stay if deleting it would lose a fact that cannot be recovered
by reading the surrounding code: the reason a choice was made, a non-obvious
constraint or ordering requirement, a named invariant, a footgun, a unit, a
worked example, or a pointer to something outside the file.

When uncertain, keep it. A wrongly deleted explanation costs more than a
surviving restatement.

Worked examples:
[1] // Loop over every row
    for r in rows:
-> not kept (the `for` already says it)
[2] // Fixed: skip None entries
    if x is None: continue
-> not kept (narrates the edit, not the reason)
[3] // Step 2: upload the metadata
    upload(meta)
-> not kept (plan step, belongs in the PR)
[4] // Guard against sentinel rows the upstream feed still emits
    if r.ts == EPOCH: continue
-> KEEP (why EPOCH is special is not in the code)
[5] // Conditional, terminal-guarded write -- the durability guarantee
    if not terminal: write()
-> KEEP (names the invariant the branch protects)
[6] // Configure the logger before any import pulls in the pipeline package
    logging.basicConfig()
-> KEEP (states an ordering constraint)
[7] // Cache duration in milliseconds (5 minutes)
    CACHE_MS = 5 * 60 * 1000
-> KEEP (supplies the unit and a human-readable value)

Return only JSON listing the ids to keep: {"keep": [ids]}"""


def classify(
    pairs: list[tuple[int, str, str]], deadline: float | None = None
) -> set[int]:
    """Ask the local model which comments are slop. Returns line numbers.

    The model is asked which comments to KEEP; anything in the batch it does not
    keep is reported. It only ever sees comment+code text and only ever returns
    ids from the batch it was given, so it cannot point at a line it was not
    shown and never emits code.

    A batch that fails is skipped entirely rather than treated as "keep
    nothing" -- with the ask inverted, an unreachable model would otherwise
    flag every comment it was about to judge.
    """
    import urllib.request

    out: set[int] = set()
    stop = None if deadline is None else time.monotonic() + deadline
    sent = 0
    for start in range(0, len(pairs), LLM_BATCH):
        if stop is not None and time.monotonic() >= stop:
            warn(
                f"{len(pairs) - sent} comments not sent to the model "
                f"(deadline {deadline:g}s reached)"
            )
            break
        chunk = pairs[start : start + LLM_BATCH]
        sent += len(chunk)
        items = "\n".join(
            f"[{i}] // {body}\n" + "\n".join(f"    {line}" for line in code)
            for i, (_, body, code) in enumerate(chunk, 1)
        )
        payload = {
            "model": LLM_MODEL,
            "temperature": 0,
            "max_tokens": 320,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": LLM_PROMPT},
                {"role": "user", "content": items},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "r",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "required": ["keep"],
                        "properties": {
                            "keep": {"type": "array", "items": {"type": "integer"}}
                        },
                    },
                },
            },
        }
        req = urllib.request.Request(
            LLM_API,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            budget = (
                LLM_TIMEOUT
                if stop is None
                else max(1.0, min(LLM_TIMEOUT, stop - time.monotonic()))
            )
            with urllib.request.urlopen(req, timeout=budget) as resp:
                body = json.load(resp)
            text = body["choices"][0]["message"]["content"] or ""
            kept = json.loads(text[text.index("{") : text.rindex("}") + 1])["keep"]
        except Exception:
            warn("local model unreachable; reporting deterministic findings only")
            continue
        keep_ids = {i for i in kept if isinstance(i, int)}
        out |= {ln for i, (ln, _, _) in enumerate(chunk, 1) if i not in keep_ids}
    return out


def warn(msg: str) -> None:
    print(f"comment-slop: {msg}", file=sys.stderr)


CONVENTION = (
    "Repo convention: comment the non-obvious why, not the what.\n"
    "Delete or rewrite these, or explain why each is load-bearing."
)


def scan(
    path: str,
    include_unreviewed: bool = False,
    use_llm: bool = False,
    deadline: float | None = None,
    whole_file: bool = False,
    ref: str = "HEAD",
) -> list[Finding]:
    if lang_key(path) not in SUPPORTED:
        return []
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return []
        with open(path, encoding="utf-8") as f:
            src = f.read()
    except (OSError, UnicodeDecodeError):
        return []
    added = None if whole_file else added_lines(path, ref)
    raw = extract(src, path)
    out = find_findings(src, path, added, include_unreviewed, raw)[:MAX_REPORT]
    counted, neutral = density_sets(raw)
    if use_llm:
        reported = {f.line for f in out}
        lines = src.split("\n")
        pairs = []
        for b in _group_blocks(raw, lines):
            ln, body = b.line, classified_body(b)
            if ln in reported or not body or body.startswith(":"):
                continue
            if NEVER.search(body) or not model_eligible(body):
                continue
            if added is not None and not (added & set(range(ln, ln + b.span))):
                continue
            # Own-line only. A trailing comment is terse by necessity, and the
            # model reads that as narration: 4 of 9 false positives on a diff.
            if b.inline:
                continue
            code = block_context(lines, b, LLM_CONTEXT_LINES)
            if code:
                pairs.append((ln, body, code[:LLM_CONTEXT_LINES]))
        if pairs:
            texts = {ln: body for ln, body, _ in pairs}
            for ln in sorted(classify(pairs, deadline)):
                out.append(Finding(ln, "narration (model)", texts[ln]))

    d = density_finding(src, path, added, counted, neutral)
    # Density is a file-level summary, so it leads; per-line findings follow in
    # line order rather than tier order.
    out.sort(key=lambda f: f.line)
    return ([d] if d else []) + out


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

# eval/ and tests/ keep their own lists: `recalibrate.py` MUST read
# site-packages, where its reference corpora live.
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
    afterwards. Over a tree of checkouts that was the difference between 40s and
    3s, which is why the corpus walkers in tests/ share this rather than keeping
    their own `rglob`.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SWEEP_SKIP_DIRS]
        out += [os.path.join(dirpath, f) for f in sorted(filenames)]
    return out


def expand(paths: list[str]) -> list[str]:
    """Files named directly, plus every supported file a pattern or a directory
    names.

    A quoted `'frontend/src/**/*.ts'` reached `os.path.getsize` as a literal
    filename and scanned nothing, so the sweep the README documented was
    silently empty. Quoting it is not optional either -- whether a bare `**`
    recurses depends on the shell -- so expanding it here is the only place it
    can happen.
    """
    import glob

    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen and lang_key(path) in SUPPORTED:
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

    blocks = []
    for p in paths:
        try:
            findings = scan(
                p,
                a.include_unreviewed,
                a.llm,
                LLM_DEADLINE if a.hook else None,
                a.whole_file,
                ref,
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
    sys.exit(main())
