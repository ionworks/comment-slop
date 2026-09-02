"""The hook must never break the edit loop. Six added lexers extend that
promise, so each gets the same pathological-input battery."""

from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs

EXTRACTORS = [
    ("py", cs.py_comments),
    ("js", cs.js_comments),
    ("yaml", cs.yaml_comments),
    ("rust", cs.rust_comments),
    ("toml", cs.toml_comments),
    ("dockerfile", cs.dockerfile_comments),
    ("shell", cs.shell_comments),
]

PATHOLOGICAL = {
    "empty": "",
    "only_newlines": "\n\n\n",
    "unterminated_block": "/* never closed\nmore\n",
    "unterminated_quote": 'let s = "never closed\n',
    "unterminated_raw": 'let r = r#"never closed\n',
    "unterminated_c_raw": 'let r = cr#"never closed\n',
    "unterminated_char": "let c = 'x\n",
    "nested_open_blocks": "/*" * 500,
    "deep_nesting": "/*" * 200 + "*/" * 200,
    "crlf": "// one\r\nlet x = 1;\r\n",
    "bom": "﻿// one\nlet x = 1;\n",
    "lone_slashes": "/" * 2000,
    "lone_hashes": "#" * 2000,
    "lone_quotes": '"' * 2000,
    "lone_ticks": "'" * 2000,
    "one_long_line": "x" * 1_000_000,
    "long_comment_line": "// " + "w " * 200_000,
    "null_bytes": "// a\x00b\nlet x = 1;\n",
    "high_unicode": "// \U0001f600 ✅ 中文\nlet x = 1;\n",
    "tabs_only": "\t\t# indented\n",
    "block_scalar_no_body": "s: |\n",
    "unterminated_multiline_basic": 'd = """never closed\n# not\n',
    "unterminated_multiline_literal": "d = '''never closed\n# not\n",
    "lone_triple_quotes": '"""' * 700,
    "quotes_against_delimiter": '"' * 6 + "\n",
    "unterminated_heredoc": "RUN <<EOF\necho hi\n# not\n",
    "heredoc_markers_only": "RUN <<" * 2000,
    "unterminated_ansi_c_quote": "echo $'never closed\n# not\n",
    "trailing_backslash": "echo one \\",
    "lone_backslashes": "\\" * 2000,
    "shell_heredoc_no_terminator": "cat <<EOF\n" + "echo x\n" * 500,
}


# Non-termination is unbounded, so this only has to catch a hang -- not a
# slowdown, which an absolute bound on a shared runner cannot see anyway.
TERMINATION_BUDGET = 60.0


@pytest.mark.parametrize("name,extractor", EXTRACTORS, ids=[e[0] for e in EXTRACTORS])
@pytest.mark.parametrize("case", sorted(PATHOLOGICAL), ids=str)
def test_extractor_never_raises_and_terminates(name, extractor, case):
    src = PATHOLOGICAL[case]
    start = time.monotonic()
    got = extractor(src)
    elapsed = time.monotonic() - start
    assert isinstance(got, list)
    assert all(isinstance(c, cs.Comment) for c in got)
    assert elapsed < TERMINATION_BUDGET, f"{name}/{case} took {elapsed:.1f}s"


@pytest.mark.parametrize(
    "ext", [".py", ".ts", ".yml", ".rs", ".toml", ".dockerfile", ".sh"]
)
def test_scan_survives_pathological_files(tmp_path, ext):
    for case, src in PATHOLOGICAL.items():
        path = tmp_path / f"{case}{ext}"
        path.write_text(src, encoding="utf-8")
        assert isinstance(cs.scan(str(path)), list)


@pytest.mark.parametrize(
    "ext", [".py", ".ts", ".yml", ".rs", ".toml", ".dockerfile", ".sh"]
)
def test_main_always_exits_zero_or_two(tmp_path, ext):
    for case, src in PATHOLOGICAL.items():
        path = tmp_path / f"{case}{ext}"
        path.write_text(src, encoding="utf-8")
        assert cs.main([str(path)]) in (0, 2)


@pytest.mark.parametrize(
    "ext", [".py", ".ts", ".yml", ".rs", ".toml", ".dockerfile", ".sh"]
)
def test_non_utf8_is_skipped(tmp_path, ext):
    path = tmp_path / f"bad{ext}"
    path.write_bytes(b"// \xff\xfe banner =====\nx = 1\n")
    assert cs.scan(str(path)) == []


def test_reported_positions_exist_in_the_source():
    """An invariant, not a case list: every reported comment must sit at a
    real position, or a finding points somewhere the author cannot look."""
    samples = {
        ".py": "# one\nx = 1  # two\n'''doc'''\n",
        ".ts": "// one\n/* two */\n/** three */\nconst x = 1;\n",
        ".yml": "# one\nkey: value  # two\n",
        ".rs": "// one\n/// doc\n/* block */\nlet x = 1;\n",
        ".toml": '# one\nkey = 1  # two\nd = """\n# not\n"""\n',
        ".sh": "# one\nset -e  # two\ncat <<EOF\n# not\nEOF\n",
        ".dockerfile": "# one\n   # two\nRUN echo '# not'\nFROM alpine\n",
    }
    for ext, src in samples.items():
        lines = src.split("\n")
        for c in cs.extract(src, f"a{ext}"):
            assert 1 <= c.line <= len(lines), (ext, c)
            assert 0 <= c.col <= len(lines[c.line - 1]), (ext, c)
            assert c.span >= 1, (ext, c)


@pytest.mark.parametrize(
    "ext,src",
    [
        (".ts", "// one\n/* two\nstill two */\n// three\nconst x = 1;\n"),
        (".rs", "// one\n/* two\nstill two */\n/// three\nlet x = 1;\n"),
        (".toml", '# one\nd = """\ntwo\n"""  # three\nk = 1  # four\n'),
        (".sh", '# one\nm="two\nthree"  # four\ncmd \\\n  arg\n# five\n'),
    ],
)
def test_positions_are_monotonic(ext, src):
    """A construct that ends mid-line used to reset col, so a later comment on
    the same line reported a column before the one it follows."""
    got = cs.extract(src, f"a{ext}")
    keys = [(c.line, c.col) for c in got]
    assert keys == sorted(keys), keys
