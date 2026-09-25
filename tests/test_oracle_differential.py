"""Differential test against tree-sitter, an independent lexer.

Golden tests prove the cases we thought of. This proves agreement with a
grammar nobody here wrote, over real files, which is the only way to find the
cases we did not think of.

A disagreement is INVESTIGATED, not assumed to be ours. Every one `ACCEPTED`
and `skip_broken` record is the grammar failing to parse a Helm template, a
CloudFormation file or a continued `ARG`, with the reason beside it.

Dev-only dependency. The engine stays stdlib-only.

Examples
--------
    just test-hooks-oracle
"""

import fnmatch
import functools
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs

# In strict mode a missing grammar or corpus FAILS rather than skipping: a suite
# that only skips gates nothing (docs/internals.md).
STRICT = os.environ.get("COMMENT_SLOP_ORACLE_STRICT") == "1"


# This checkout, so TSX/TS/YAML need no setup and CI needs no clone. The
# detector's own walk-up, not a parent count, which silently retargets a move.
REPO_ROOT = Path(cs.repo_root(str(Path(__file__).resolve().parent)))
CHECKOUTS = Path(os.path.expanduser("~/Documents/git-repos"))


def _root(env: str, default) -> Path:
    """An env override, treating empty as unset.

    An exported-but-empty variable must not count as a corpus: `Path("")` is
    `Path(".")` -- a directory that exists and holds none of the files asked
    for, which in strict mode fails as a broken promise rather than reading as
    "not set". Opting a corpus OUT is `-k`, not an empty variable; see the
    `tree-sitter differential` step in ci-hooks.yml.
    """
    return Path(os.environ.get(env) or default)


APP = _root("COMMENT_SLOP_ORACLE_APP", REPO_ROOT)
# TOML and Dockerfiles are a handful per repo, so their corpus widens to every
# local checkout, falling back to this one: 13 `.toml` here against 200+.
REPOS = _root(
    "COMMENT_SLOP_ORACLE_REPOS", CHECKOUTS if CHECKOUTS.is_dir() else REPO_ROOT
)
# The one corpus with no in-repo fallback, this not being a Rust project.
# `test_rust_traps` covers the known traps inline, so CI is not blind.
DIFFSOL = _root("COMMENT_SLOP_ORACLE_RUST", CHECKOUTS / "diffsol")

# Generated bundles are not source, and the one known lexer disagreement lives
# there. Excluded by shape, so a bundle committed anywhere is caught.
MINIFIED_COLUMN = 500

# A differential that reads two files and passes has told you nothing, so strict
# mode holds each comparison to a floor.

# TOML and Dockerfile floors are low because this repo holds 13 and 5, and
# `skip_broken` drops unparseable ones. COMMENT_SLOP_ORACLE_REPOS raises them.
MIN_FILES = {
    "rs": 20,
    "tsx": 100,
    "ts": 100,
    "yaml": 20,
    "toml": 5,
    "docker": 2,
    "shell": 20,
}


def unavailable(reason: str):
    """Skip, or fail when the caller promised the corpus would be there."""
    if STRICT:
        pytest.fail(f"strict mode: {reason}")
    pytest.skip(reason)


def is_minified(src: str) -> bool:
    return any(len(line) > MINIFIED_COLUMN for line in src.split("\n"))


# Files where tree-sitter is the one that is wrong. Each entry names the
# construct that defeats the grammar.
ACCEPTED = (
    "templates/",  # Helm: {{- ... -}} is not YAML
    "anyscale-cloud-setup",  # CloudFormation !Ref tags defeat tree-sitter-yaml
)


def ts_parser(module_name: str, attr: str = "language"):
    try:
        import tree_sitter  # noqa: F401

        mod = __import__(module_name)
    except ImportError as exc:
        unavailable(
            f"{exc}. Run `just test-hooks-oracle`, which installs the "
            f"grammars; plain `just test-hooks` does not."
        )
    from tree_sitter import Language, Parser

    return Parser(Language(getattr(mod, attr)()))


def oracle(parser, src: bytes, kinds: set[str]) -> set[tuple[int, int]]:
    """(line, col) of every comment node tree-sitter finds.

    `start_point[1]` is a byte offset; our lexers index the decoded string, so
    a comment after an em dash on the same line sits two columns apart in the
    two units. Convert to characters here rather than pretending they agree.
    """
    lines = src.split(b"\n")
    out, stack = set(), [parser.parse(src).root_node]
    while stack:
        node = stack.pop()
        if node.type in kinds:
            row, byte_col = node.start_point
            col = len(lines[row][:byte_col].decode("utf-8", "replace"))
            out.add((row + 1, col))
        stack.extend(node.children)
    return out


@functools.cache
def _matching(root: Path, patterns: tuple[str, ...]) -> tuple[Path, ...]:
    """Cached, because `TestTomlOracle` and `TestDockerfileOracle` both draw
    from `REPOS` -- a tree of every local checkout, where one walk is the
    expensive part of this module. Kept apart from `corpus` so `unavailable`
    stays outside the cache."""
    return tuple(
        sorted(
            Path(p)
            for p in cs.walk_source(str(root))
            if any(fnmatch.fnmatch(os.path.basename(p), pat) for pat in patterns)
        )
    )


def corpus(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    """EVERY matching file under `root`. No sampling -- a differential that
    samples is a differential that can be lucky, and docs/internals.md records the
    one file in 555 that proved it.

    Excluded directories are `cs.SWEEP_SKIP_DIRS`, via `cs.walk_source`: a
    directory production does not sweep is one the oracle need not certify, and
    pruning the descent rather than filtering the results is what makes reading
    every file affordable.
    """
    if not root.is_dir():
        unavailable(f"corpus absent: {root}")
    files = _matching(root, patterns)
    if not files:
        unavailable(f"no files matching {patterns} under {root}")
    return list(files)


def line_initial(positions, src):
    """Only the oracle hits that are the first thing on their line.

    tree-sitter-dockerfile parses the shell inside a `RUN` and tags a `#` there
    as a comment. Docker does not -- only a line-initial `#` is comment syntax,
    and the shell is what reads the rest -- so this scopes the oracle to the
    same question the extractor answers, not to a different one.
    """
    lines = src.split("\n")
    return {(ln, col) for ln, col in positions if not lines[ln - 1][:col].strip()}


def compare(parser, files, extractor, kinds, scope=None, skip_broken=False, floor=None):
    """Assert our (line, col) set matches the oracle's for every file."""
    mismatches = []
    read = 0
    for path in files:
        if any(a in str(path) for a in ACCEPTED):
            continue
        raw = path.read_bytes()
        if len(raw) > cs.MAX_BYTES:
            continue
        try:
            src = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if is_minified(src):
            continue
        read += 1
        # A grammar that could not parse the file has no opinion worth taking.
        if skip_broken and parser.parse(raw).root_node.has_error:
            continue
        ours = {(c.line, c.col) for c in extractor(src)}
        theirs = oracle(parser, raw, kinds)
        if scope is not None:
            theirs = scope(theirs, src)
        if ours != theirs:
            mismatches.append(
                f"{path}\n  only ours:   {sorted(ours - theirs)[:6]}"
                f"\n  only oracle: {sorted(theirs - ours)[:6]}"
            )
    assert not mismatches, (
        f"{len(mismatches)} of {read} file(s) disagree with tree-sitter. Read "
        f"each one before changing the lexer -- the oracle is sometimes the "
        f"wrong party:\n" + "\n".join(mismatches[:5])
    )
    if floor is not None and read < floor:
        unavailable(
            f"compared only {read} files, floor is {floor}; the corpus "
            f"resolved to somewhere almost empty"
        )
    return read


class TestRustOracle:
    def test_agrees_on_real_rust(self):
        """The only corpus outside this checkout, so the only one that skips in
        CI. Point COMMENT_SLOP_ORACLE_RUST at a Rust checkout to run it; the
        `test_rust_traps` case below carries its source inline and runs
        everywhere."""
        compare(
            ts_parser("tree_sitter_rust"),
            corpus(DIFFSOL, ("*.rs",)),
            cs.rust_comments,
            {"line_comment", "block_comment"},
            floor=MIN_FILES["rs"],
        )


class TestYamlOracle:
    def test_agrees_on_real_yaml(self):
        compare(
            ts_parser("tree_sitter_yaml"),
            corpus(APP, ("*.yml", "*.yaml")),
            cs.yaml_comments,
            {"comment"},
            floor=MIN_FILES["yaml"],
        )


class TestTomlOracle:
    def test_agrees_on_real_toml(self):
        compare(
            ts_parser("tree_sitter_toml"),
            corpus(REPOS, ("*.toml",)),
            cs.toml_comments,
            {"comment"},
            floor=MIN_FILES["toml"],
        )


class TestDockerfileOracle:
    def test_agrees_on_real_dockerfiles(self):
        """Dockerfiles are a handful per repo, so the corpus is repo-wide. 41
        of 185 are skipped for a parse error the grammar has on a continued
        `ARG`; the remaining 144 agree exactly."""
        compare(
            ts_parser("tree_sitter_dockerfile"),
            corpus(
                REPOS,
                (
                    "Dockerfile",
                    "Dockerfile.*",
                    "*.dockerfile",
                    "Containerfile",
                    "Containerfile.*",
                ),
            ),
            cs.dockerfile_comments,
            {"comment"},
            scope=line_initial,
            skip_broken=True,
            floor=MIN_FILES["docker"],
        )


class TestShellOracle:
    def test_agrees_on_real_shell(self):
        """Shell is the one `#` language with a trailing comment form, quoting
        that spans lines and heredocs, so the grammar is worth more here than
        anywhere else: a line-continuation slip shifts every later finding in a
        script one line up, and no golden test would think to look."""
        compare(
            ts_parser("tree_sitter_bash"),
            corpus(APP, ("*.sh", "*.bash", "*.zsh", "*.ksh")),
            cs.shell_comments,
            {"comment"},
            floor=MIN_FILES["shell"],
        )


class TestTypeScriptOracle:
    def test_agrees_on_real_tsx(self):
        compare(
            ts_parser("tree_sitter_typescript", "language_tsx"),
            corpus(APP / "frontend" / "src", ("*.tsx", "*.jsx")),
            cs.js_comments,
            {"comment"},
            floor=MIN_FILES["tsx"],
        )

    def test_agrees_on_real_typescript(self):
        """`.ts` is a separate grammar because it is a separate language here:
        TypeScript forbids JSX in it so `<Foo>x` can stay a type assertion, and
        the `ts` language turns the JSX scanner off to match. Without its own
        grammar here, nothing would notice the extractor reading both the same
        way. Dispatched through the registry, which is what production calls."""
        compare(
            ts_parser("tree_sitter_typescript", "language_typescript"),
            corpus(APP / "frontend" / "src", ("*.ts",)),
            cs.language_of("a.ts").extract,
            {"comment"},
            floor=MIN_FILES["ts"],
        )


class TestOracleCatchesTheKnownTraps:
    """Sanity check on the oracle itself: if these stop being traps the
    differential above has lost its teeth."""

    def test_rust_traps(self):
        parser = ts_parser("tree_sitter_rust")
        src = (
            b"fn f<'a>(s: &'a str) {\n"
            b'    let x = "// not";\n'
            b'    let r = r#"also /* not */"#;\n'
            b'    let b = b"esc \\" // not";\n'
            b"    /* outer /* nested */ still */\n"
            b"    let c = '/';  // real\n"
            b"}\n"
        )
        ours = {(c.line, c.col) for c in cs.rust_comments(src.decode())}
        assert ours == oracle(parser, src, {"line_comment", "block_comment"})

    def test_toml_traps(self):
        parser = ts_parser("tree_sitter_toml")
        src = (
            b"x = 1# no space needed\n"
            b'd = """\n'
            b"# not a comment\n"
            b'"""  # real\n'
            b"l = '''\n"
            b"# also not\n"
            b"'''\n"
            b'"key # not" = 1\n'
            b"lit = 'a\\'  # real\n"
            b'esc = "a \\" # not"\n'
            b"# own line\n"
        )
        ours = {(c.line, c.col) for c in cs.toml_comments(src.decode())}
        assert ours == oracle(parser, src, {"comment"})

    def test_dockerfile_traps(self):
        parser = ts_parser("tree_sitter_dockerfile")
        src = (
            b"# syntax=docker/dockerfile:1\n"
            b"FROM alpine\n"
            b"RUN curl https://x.example/#frag\n"
            b'LABEL note="a # b"\n'
            b"RUN <<EOF\n"
            b"# not a comment\n"
            b"echo hi\n"
            b"EOF\n"
            b"RUN apk add gcc \\\n"
            b"# real, inside a continuation\n"
            b"    binutils\n"
            b"   # real, indented\n"
        )
        ours = {(c.line, c.col) for c in cs.dockerfile_comments(src.decode())}
        assert ours == line_initial(oracle(parser, src, {"comment"}), src.decode())

    def test_tsx_traps(self):
        """JSX text is not code, and `<T extends U>` is not an element."""
        parser = ts_parser("tree_sitter_typescript", "language_tsx")
        src = (
            b"const f = <K extends keyof C>(k: K) => k;\n"
            b"const g = <A, B>(a: A) => a;\n"
            b"export default function P() {\n"
            b"  return (\n"
            b'    <Box component="pre">\n'
            b"      cp -r ~/ionworks-skills/skills/* ~/.claude/skills/\n"
            b"      it's 5 < 6 and 7 > 6, // not a comment\n"
            b"      {/* real */}\n"
            b"    </Box>\n"
            b"  );\n"
            b"}\n"
            b"// real\n"
        )
        ours = {(c.line, c.col) for c in cs.js_comments(src.decode())}
        assert ours == oracle(parser, src, {"comment"})

    def test_shell_traps(self):
        """The shebang counts as a comment to both parties; `>#hash` opens one
        to bash as well, which is why that line is a syntax error rather than a
        redirect."""
        parser = ts_parser("tree_sitter_bash")
        src = (
            b"#!/usr/bin/env bash\n"
            b"set -eu  # real\n"
            b'echo "a # not"  ${x#not}  $((16#ff))  it#not\n'
            b"echo $'esc \\' # not'\n"
            b"cat <<-EOF\n"
            b"# not a comment\n"
            b"\tEOF\n"
            b'cat <<<"$x"  # real, a herestring has no body\n'
            b"cmd one \\\n"
            b"    two\n"
            b"echo hi;# real\n"
            b'msg="line one\n'
            b'# not, inside the string"\n'
            b"# real\n"
        )
        ours = {(c.line, c.col) for c in cs.shell_comments(src.decode())}
        assert ours == oracle(parser, src, {"comment"})

    def test_yaml_traps(self):
        parser = ts_parser("tree_sitter_yaml")
        src = (
            b"key: value  # real\n"
            b"literal: |2\n"
            b"   # not a comment\n"
            b"\n"
            b"   still body\n"
            b'quoted: "# not"\n'
            b"# own line\n"
        )
        ours = {(c.line, c.col) for c in cs.yaml_comments(src.decode())}
        assert ours == oracle(parser, src, {"comment"})
