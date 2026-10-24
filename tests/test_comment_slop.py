"""Golden cases for the comment-slop detector."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import comment_slop as cs


class TestPyComments:
    def test_finds_line_and_col_and_body(self):
        src = "x = 1  # add one\n"
        assert cs.py_comments(src) == [cs.Comment(1, 7, ("add one",))]

    def test_own_line_comment(self):
        src = "# banner\nx = 1\n"
        assert cs.py_comments(src) == [cs.Comment(1, 0, ("banner",))]

    def test_docstrings_are_doc_comments_not_line_comments(self):
        """Docstrings enter the pipeline as `doc`, which classifies them on
        their narrative prologue rather than as line comments."""
        src = '"""Module docstring."""\n\n\ndef f():\n    """Doc."""\n    return 1\n'
        assert cs._py_line_comments(src) == []
        assert [c.kind for c in cs.py_comments(src)] == [cs.DOC, cs.DOC]

    def test_hash_inside_string_is_not_a_comment(self):
        src = 's = "# not a comment"\nt = f"{x} # also not"\n'
        assert cs.py_comments(src) == []

    def test_strips_multiple_markers_and_whitespace(self):
        src = "##   spaced   \n"
        assert cs.py_comments(src) == [cs.Comment(1, 0, ("spaced",))]

    def test_syntax_error_keeps_the_comments_already_tokenised(self):
        """Returning [] on a TokenError would silence the hook on exactly the
        incomplete syntax a write-time hook sees most."""
        assert cs.py_comments("def f(:\n  # c\n") == [cs.Comment(2, 2, ("c",))]

    def test_unterminated_string_returns_empty(self):
        assert cs.py_comments('s = "open\n# c\n') == []

    def test_multiple_comments_in_order(self):
        src = "# one\nx = 1  # two\n# three\n"
        assert cs.py_comments(src) == [
            cs.Comment(1, 0, ("one",)),
            cs.Comment(2, 7, ("two",)),
            cs.Comment(3, 0, ("three",)),
        ]


class TestJsComments:
    def test_finds_line_comment(self):
        src = "const x = 1; // add one\n"
        assert cs.js_comments(src) == [cs.Comment(1, 13, ("add one",))]

    def test_slash_slash_inside_regex_literal_is_not_a_comment(self):
        src = "const re = /https?:\\/\\//;\nconst y = 2;\n"
        assert cs.js_comments(src) == []

    def test_slash_slash_inside_string_is_not_a_comment(self):
        src = 'const u = "http://x.com"; // real\n'
        assert cs.js_comments(src) == [cs.Comment(1, 26, ("real",))]

    def test_slash_slash_inside_template_is_not_a_comment(self):
        src = "const t = `no // comment here`;\n"
        assert cs.js_comments(src) == []

    def test_block_comments_are_extracted(self):
        """Skipping block comments entirely is what let an 11-line JSDoc
        block through."""
        src = "/* block\n   spans lines */\nconst x = 1; // after\n"
        assert cs.js_comments(src) == [
            cs.Comment(1, 0, ("block", "spans lines"), cs.PLAIN, mergeable=False),
            cs.Comment(3, 13, ("after",)),
        ]

    def test_bang_comment_is_skipped(self):
        src = "//! preserved banner\nconst x = 1;\n"
        assert cs.js_comments(src) == []

    def test_division_is_not_a_regex(self):
        src = "const r = a / b; // ratio\n"
        assert cs.js_comments(src) == [cs.Comment(1, 17, ("ratio",))]

    def test_escaped_newline_in_string_keeps_line_numbers(self):
        src = 'const s = "one\\\ntwo";\nconst x = 1; // here\n'
        assert cs.js_comments(src) == [cs.Comment(3, 13, ("here",))]

    def test_line_numbers_survive_multiline_template(self):
        src = "const t = `a\nb\nc`;\nconst x = 1; // here\n"
        assert cs.js_comments(src) == [cs.Comment(4, 13, ("here",))]

    def test_comment_at_start_of_line(self):
        src = "// top\nconst x = 1;\n"
        assert cs.js_comments(src) == [cs.Comment(1, 0, ("top",))]


class TestWords:
    def test_splits_camel_and_snake_and_drops_stopwords(self):
        assert cs.words("Skip the emptyLines in max_retries") == {
            "skip",
            "empty",
            "lines",
            "max",
            "retries",
        }

    def test_drops_single_chars(self):
        assert cs.words("a b cd") == {"cd"}


class TestRestates:
    def test_subset_of_code_words_restates(self):
        assert cs.restates("skip empty lines", "if not line.strip():") is False
        assert cs.restates("add to n", "n += i") is False
        assert cs.restates("sort items", "items.sort()") is True

    def test_words_absent_from_code_do_not_restate(self):
        assert cs.restates("guard against sentinel rows", "if i is None:") is False

    def test_string_literals_in_code_are_ignored(self):
        assert cs.restates("set name", 'obj = {"name": "set"}') is False

    def test_long_comment_never_restates(self):
        long = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        assert cs.restates(long, long) is False


class TestNeverVeto:
    def test_lint_directives_are_vetoed(self):
        for body in [
            "noqa: E501",
            "type: ignore[arg-type]",
            "ruff: noqa",
            "eslint-disable-line",
            "pyright: ignore",
            "fmt: off",
        ]:
            assert cs.NEVER.search(body), body

    def test_intent_markers_are_vetoed(self):
        for body in ["TODO: fix", "SAFETY: locked", "NOTE: careful", "HACK"]:
            assert cs.NEVER.search(body), body

    def test_references_are_vetoed(self):
        for body in ["see https://x.com/y", "fixes ABC-123", "closes #42", "RFC 7231"]:
            assert cs.NEVER.search(body), body

    def test_ordinary_prose_is_not_vetoed(self):
        assert not cs.NEVER.search("bump the count")

    def test_a_marker_word_in_prose_is_not_a_marker(self):
        """Every marker is also an ordinary English word, and IGNORECASE would
        read `a real .tsx lexer bug sat in` as a `BUG:` marker.

        A veto is worse than a wrong tier here: the comment is dropped before
        any tier sees it, and it splits its block, so the neighbours are
        measured as a shorter run than the author wrote.
        """
        for body in [
            "a real .tsx lexer bug sat in committed code",
            "note that the caller already holds it",
            "the safety of the lock is the caller's",
            "a security check runs before this",
            "this is an important special case",
            "Todo list rendering",
        ]:
            assert not cs.NEVER.search(body), body

    def test_uppercase_markers_still_veto(self):
        for body in ["BUG: broken", "NOTE: careful", "SAFETY: locked", "XXX"]:
            assert cs.NEVER.search(body), body

    def test_a_hex_colour_is_not_an_issue_reference(self):
        """`#020E0D` read as issue #020, and the veto split the palette comment
        it sat in."""
        for body in [
            "Brand primary is dark green-black (#020E0D). That works as a",
            "the accent is #0C68E9 in both modes",
        ]:
            assert not cs.NEVER.search(body), body
        assert cs.NEVER.search("removed in #256 and no longer defined")

    def test_an_encoding_or_hash_name_is_not_an_issue_key(self):
        """`UTF-8` was 18 of 24 blocks this repo exempted on the key form alone."""
        for body in [
            "most vendor files are not UTF-8, so decode as latin-1",
            "the database keeps only a SHA-256 hash and the prefix",
        ]:
            assert not cs.NEVER.search(body), body
        assert cs.NEVER.search("fixes ABC-123")

    def test_a_region_name_is_not_an_issue_key(self):
        """`east-1` would match the issue-key form under IGNORECASE."""
        for body in [
            "one subnet per us-east-1 zone",
            "US-EAST-1E offers no Graviton",
            "the eu-west-2 pool is cheaper",
        ]:
            assert not cs.NEVER.search(body), body


class TestFindFindings:
    def test_banner_is_flagged(self):
        src = "# ===== TOTALS =====\nx = 1\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [(1, "banner")]

    def test_edit_narration_is_flagged(self):
        src = "# Fixed: skip None entries\nx = 1\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [(1, "edit-narration")]

    def test_section_label_is_flagged(self):
        src = "# Helpers\nx = 1\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [(1, "section-label")]

    def test_emoji_is_flagged(self):
        src = "x = 1  # done ✅\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [(1, "emoji")]

    def test_restatement_is_flagged(self):
        src = "# sort items\nitems.sort()\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [(1, "restates-code")]

    def test_short_unmatched_comment_is_unreviewed_only_when_opted_in(self):
        src = "x = 1  # bump the count\n"
        assert cs.analyze(src, "a.py", None) == []
        f = cs.analyze(src, "a.py", None, unreviewed=True)
        assert [(i.line, i.reason) for i in f] == [(1, "unreviewed")]

    def test_long_comment_is_not_flagged(self):
        body = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu"
        f = cs.analyze(f"x = 1  # {body}\n", "a.py", None)
        assert f == []

    def test_noqa_is_never_flagged(self):
        f = cs.analyze("x = 1  # noqa: E501\n", "a.py", None)
        assert f == []

    def test_shebang_is_not_flagged(self):
        f = cs.analyze("#!/usr/bin/env python3\nx = 1\n", "a.py", None)
        assert f == []

    def test_added_set_restricts_findings(self):
        src = "# ===== ONE =====\nx = 1\n# ===== TWO =====\n"
        f = cs.analyze(src, "a.py", {3})
        assert [i.line for i in f] == [3]

    def test_empty_added_set_yields_nothing(self):
        f = cs.analyze("# ===== X =====\n", "a.py", set())
        assert f == []

    def test_form_feed_does_not_shift_target_resolution(self):
        src = "\f\n# sort items\nreturn None\n"
        f = cs.analyze(src, "a.py", None, unreviewed=True)
        assert [(i.line, i.reason) for i in f] == [(2, "unreviewed")]

    def test_js_path_uses_js_extractor(self):
        src = "// Helpers\nconst x = 1;\n"
        f = cs.analyze(src, "a.ts", None)
        assert [(i.line, i.reason) for i in f] == [(1, "section-label")]


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def commit_all(repo: Path, message: str = "wip") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


class TestAddedLines:
    def test_untracked_file_returns_none(self, tmp_path):
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text("x = 1\n")
        assert cs.added_lines(str(f)) is None

    def test_no_git_repo_returns_none(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert cs.added_lines(str(f)) is None

    def test_unmodified_tracked_file_returns_empty_set(self, tmp_path):
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text("x = 1\n")
        commit_all(repo)
        assert cs.added_lines(str(f)) == set()

    def test_appended_lines_are_reported(self, tmp_path):
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text("x = 1\n")
        commit_all(repo)
        f.write_text("x = 1\ny = 2\nz = 3\n")
        assert cs.added_lines(str(f)) == {2, 3}

    def test_inserted_line_is_reported(self, tmp_path):
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text("x = 1\nz = 3\n")
        commit_all(repo)
        f.write_text("x = 1\n# banner\nz = 3\n")
        assert cs.added_lines(str(f)) == {2}

    def test_missing_file_returns_none(self, tmp_path):
        assert cs.added_lines(str(tmp_path / "nope.py")) is None

    def test_a_relative_path_still_resolves_the_diff(self, tmp_path, monkeypatch):
        """Relative to the file's own directory, `git -C sub -- sub/a.py` would
        match nothing and lose the diff."""
        repo = make_repo(tmp_path)
        (repo / "sub").mkdir()
        f = repo / "sub" / "a.py"
        f.write_text("x = 1\n")
        commit_all(repo)
        f.write_text("x = 1\ny = 2\n")
        monkeypatch.chdir(repo)
        assert cs.added_lines("sub/a.py") == {2}

    def test_an_inherited_git_dir_does_not_lose_the_diff(self, tmp_path, monkeypatch):
        """git exports GIT_DIR to every hook, and it takes cwd for the worktree.

        Since the diff runs from the file's own directory, an inherited GIT_DIR
        would make every path resolve to nothing: narrowing would widen to a
        whole-file scan and blame a commit for lines it never touched.
        """
        repo = make_repo(tmp_path)
        (repo / "sub").mkdir()
        f = repo / "sub" / "a.py"
        f.write_text("x = 1\n")
        commit_all(repo)
        f.write_text("x = 1\ny = 2\n")
        monkeypatch.setenv("GIT_DIR", str(repo / ".git"))

        assert cs.added_lines(str(f)) == {2}


class TestScan:
    def test_non_source_extension_is_skipped(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("# ===== X =====\n")
        assert cs.scan(str(f)) == []

    def test_oversized_file_is_skipped(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("# ===== X =====\n" + "x = 1\n" * 200_000)
        assert f.stat().st_size > cs.MAX_BYTES
        assert cs.scan(str(f)) == []

    def test_missing_file_is_skipped(self, tmp_path):
        assert cs.scan(str(tmp_path / "nope.py")) == []

    def test_non_utf8_file_is_skipped(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_bytes(b"# \xff\xfe banner =====\nx = 1\n")
        assert cs.scan(str(f)) == []

    def test_untracked_file_scans_all_lines(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("# ===== TOTALS =====\nx = 1\n")
        assert [i.reason for i in cs.scan(str(f))] == ["banner"]

    def test_report_is_capped(self, tmp_path):
        f = tmp_path / "a.py"
        # Interleave code so each comment is its own block, not one merged run.
        f.write_text("".join(f"# Fixed: thing {i}\nx = {i}\n" for i in range(40)))
        per_line = [i for i in cs.scan(str(f)) if i.reason != "density"]
        assert len(per_line) == cs.MAX_REPORT


class TestReport:
    def test_contains_path_line_action_reason_and_text(self):
        findings = [cs.Finding(42, "banner", "===== SIZING =====", cs.DELETE)]
        text = cs.report("a/b.py", findings)
        assert "comment-slop: a/b.py" in text
        assert "L42" in text
        assert "delete" in text
        assert "[banner]" in text
        assert "===== SIZING =====" in text

    def test_a_shortening_finding_shows_the_part_to_cut(self):
        """The author keeps the comment, so the row names what to take out of it."""
        f = cs.Finding(
            7,
            "historical",
            "Batches stay small. Previously it sent one.",
            cs.SHORTEN,
            "Previously it sent one.",
        )
        row = cs.report("a.py", [f]).splitlines()[1]
        assert "shorten" in row and "cut: Previously it sent one." in row
        assert "Batches stay small" not in row

    def test_long_comment_text_is_truncated(self):
        findings = [cs.Finding(1, "unreviewed", "x" * 200, cs.DELETE)]
        assert "x" * 61 not in cs.report("a.py", findings)


class TestMain:
    def test_clean_file_exits_zero_and_prints_nothing(self, tmp_path, capsys):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert cs.main([str(f)]) == 0
        assert capsys.readouterr().err == ""

    def test_findings_exit_two_with_report_on_stderr(self, tmp_path, capsys):
        f = tmp_path / "a.py"
        f.write_text("# ===== TOTALS =====\nx = 1\n")
        assert cs.main([str(f)]) == 2
        err = capsys.readouterr().err
        assert "comment-slop" in err
        assert "[banner]" in err
        assert "non-obvious why" in err

    def test_no_paths_exits_zero(self):
        assert cs.main([]) == 0

    def test_hook_mode_reads_payload(self, tmp_path, capsys, monkeypatch):
        f = tmp_path / "a.py"
        f.write_text("# ===== TOTALS =====\nx = 1\n")
        payload = {"tool_name": "Write", "tool_input": {"file_path": str(f)}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert cs.main(["--hook"]) == 2
        assert "[banner]" in capsys.readouterr().err

    def test_hook_mode_malformed_payload_exits_zero(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
        assert cs.main(["--hook"]) == 0
        assert capsys.readouterr().err == ""

    def test_hook_mode_missing_file_path_exits_zero(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash"})))
        assert cs.main(["--hook"]) == 0
        assert capsys.readouterr().err == ""

    def test_multiple_paths_reported_together(self, tmp_path, capsys):
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("# ===== A =====\n")
        b.write_text("# ===== B =====\n")
        assert cs.main([str(a), str(b)]) == 2
        err = capsys.readouterr().err
        assert "a.py" in err and "b.py" in err


class TestStrict:
    """`--strict`, or COMMENT_SLOP_STRICT in the environment: the environment is
    what reaches both a session's hook and a commit's gate."""

    WHY = "    # The writer holds the lock for one poll interval at most.\n"
    BEFORE = "def f():\n" + WHY + "    wait_for(lock)\n"

    def repo_with(self, tmp_path, src="def f():\n    wait_for(lock)\n"):
        repo = make_repo(tmp_path)
        (repo / "sub").mkdir()
        f = repo / "sub" / "a.py"
        f.write_text(src)
        commit_all(repo)
        return f

    def test_the_flag_reports_a_new_comment_and_says_why(self, tmp_path, capsys):
        f = self.repo_with(tmp_path)
        f.write_text(self.BEFORE)
        assert cs.main([str(f)]) == 0
        assert cs.main(["--strict", str(f)]) == 2
        err = capsys.readouterr().err
        assert "[strict]" in err
        assert cs.STRICT_CONVENTION in err

    def test_the_strict_convention_needs_a_strict_finding(self, tmp_path, capsys):
        f = tmp_path / "a.py"
        f.write_text('def f():\n    """Fixed: skip None entries."""\n')
        assert cs.main(["--strict", str(f)]) == 2
        err = capsys.readouterr().err
        assert "[edit-narration]" in err
        assert cs.STRICT_CONVENTION not in err

    @pytest.mark.parametrize("value, code", [("1", 2), ("0", 0), ("", 0)])
    def test_the_environment_turns_it_on(self, tmp_path, monkeypatch, value, code):
        f = self.repo_with(tmp_path)
        f.write_text(self.BEFORE)
        monkeypatch.setenv("COMMENT_SLOP_STRICT", value)
        assert cs.main([str(f)]) == code

    def test_the_hook_reads_the_environment(self, tmp_path, capsys, monkeypatch):
        f = self.repo_with(tmp_path)
        f.write_text(self.BEFORE)
        payload = {"tool_input": {"file_path": str(f)}}
        monkeypatch.setenv("COMMENT_SLOP_STRICT", "1")
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert cs.main(["--hook"]) == 2
        assert "[strict]" in capsys.readouterr().err

    def test_a_committed_comment_reindented_is_not_new(self, tmp_path, monkeypatch):
        """Read through git, from a relative path in a subdirectory, the way
        `added_lines` resolves the same file."""
        f = self.repo_with(tmp_path, self.BEFORE)
        f.write_text(
            "def f():\n    if ready:\n    " + self.WHY + "        wait_for(lock)\n"
        )
        monkeypatch.chdir(tmp_path)
        assert cs.added_lines("sub/a.py") == {2, 3, 4}
        assert cs.scan("sub/a.py", strict=True) == []

    def test_a_renamed_file_is_compared_with_its_old_path(self, tmp_path, capsys):
        """Into another directory, so the old and new paths share no prefix."""
        self.repo_with(tmp_path, self.BEFORE)
        (tmp_path / "other").mkdir()
        subprocess.run(
            ["git", "mv", "sub/a.py", "other/b.py"], cwd=tmp_path, check=True
        )
        moved = tmp_path / "other" / "b.py"
        moved.write_text(self.BEFORE + "\n\ndef g():\n    return 1\n")
        assert cs.scan(str(moved), strict=True) == []
        # Only the line the change added is new, not the comment it extends.
        moved.write_text(
            "def f():\n" + self.WHY + "    # Polls until the writer lets go.\n"
            "    wait_for(lock)\n"
        )
        assert cs.main(["--strict", str(moved)]) == 2
        assert "cut: Polls until the writer lets go." in capsys.readouterr().err

    def test_a_committed_comment_pasted_again_is_new(self, tmp_path, capsys):
        f = self.repo_with(tmp_path, self.BEFORE)
        f.write_text(self.BEFORE + "\n\n" + self.BEFORE.replace("f()", "g()"))
        assert cs.main(["--strict", str(f)]) == 2
        assert "L7    delete  [strict]" in capsys.readouterr().err

    def test_since_compares_against_that_ref(self, tmp_path, capsys):
        f = self.repo_with(tmp_path, self.BEFORE)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        f.write_text(
            "def f():\n" + self.WHY + "    # Polls until the writer lets go.\n"
            "    wait_for(lock)\n"
        )
        commit_all(tmp_path)
        assert cs.main(["--strict", str(f)]) == 0
        assert cs.main(["--strict", "--since", base, str(f)]) == 2
        assert "cut: Polls until the writer lets go." in capsys.readouterr().err


class TestBlockGrouping:
    def test_wrapped_comment_is_one_finding_at_its_first_line(self):
        src = (
            "# Now the held terminal events are replayed in order, because the\n"
            "# client may have missed them while reconnecting.\n"
            "flush()\n"
        )
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [(1, "narrative")]

    def test_continuation_fragment_is_not_judged_alone(self):
        """Judged alone, "next turn." reads as narrative slop."""
        src = (
            "# The transport owns the temp dir so it survives until the\n"
            "# next turn.\n"
            "keep()\n"
        )
        assert cs.analyze(src, "a.py", None) == []

    def test_delimiters_stay_standalone_around_a_label(self):
        src = (
            "# ---------------\n"
            "# validate_source_fks - the request-model validator\n"
            "# ---------------\n"
            "def test_x():\n    pass\n"
        )
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f if i.reason != "density"] == [
            (1, "banner"),
            (3, "banner"),
        ]

    def test_inline_comments_never_merge(self):
        src = "a = 1  # Fixed: one\nb = 2  # Fixed: two\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.line for i in f] == [1, 2]


class TestBannerPrecision:
    def test_pure_divider_is_a_banner(self):
        f = cs.analyze("# " + "-" * 60 + "\nx = 1\n", "a.py", None)
        assert [i.reason for i in f] == ["banner"]

    def test_delimiter_wrapped_label_is_a_banner(self):
        f = cs.analyze("# ===== TOTALS =====\nx = 1\n", "a.py", None)
        assert [i.reason for i in f] == ["banner"]

    def test_long_delimiter_wrapped_label_is_a_banner(self):
        """A display-width cap would hide structural banners with descriptive names."""
        src = (
            "# --- Tests for ExternalServiceError wrapping of job submission ---\n"
            "def test_submit():\n    pass\n"
        )
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["banner"]

    def test_dashes_followed_by_real_content_is_not_a_banner(self):
        src = (
            "# ---- object-store broadcast term (the ray.put context, "
            "one copy per node)\nx = 1\n"
        )
        assert cs.analyze(src, "a.py", None) == []


class TestEditNarrationPrecision:
    """The tier reports a completed edit, not the imperative mood.

    Corpus cases 107 and 121 are labelled `keep`, and an imperative arm makes
    them 2 of the 6 false positives: an imperative opening is how a numpydoc
    summary and a step comment say what the code does.
    """

    def test_past_tense_narrates_an_edit(self):
        for body in [
            "Removed per-measurement delete from instance context",
            "Updated to return a dict",
            "Reworked to avoid the extra round-trip",
            "Switched to the pooled client",
        ]:
            f = cs.analyze(f"# {body}\nx = 1\n", "a.py", None)
            assert [i.reason for i in f] == ["edit-narration"], body

    def test_imperative_describes_the_code(self):
        for body in [
            "Remove helper columns if they weren't in the original request",
            "Update with capacity and esoh parameters",
            "Extract step number from the jump marker",
            "Drop cycle_column so the output does not duplicate it",
        ]:
            assert cs.analyze(f"# {body}\nx = 1\n", "a.py", None) == [], body

    def test_changelog_prefix_narrates_whatever_its_tense(self):
        for body in ["Fix: skip None entries", "New: pooled transport"]:
            f = cs.analyze(f"# {body}\nx = 1\n", "a.py", None)
            assert [i.reason for i in f] == ["edit-narration"], body

    def test_numpydoc_summary_is_not_an_edit(self):
        """Every CRUD docstring in a FastAPI backend opens this way."""
        src = (
            "def update_program(program_id, data):\n"
            '    """Update an existing program by ID."""\n'
            "    return _update(program_id, data)\n"
        )
        assert cs.analyze(src, "a.py", None) == []


class TestEmojiPrecision:
    def test_arrow_in_prose_is_not_emoji(self):
        src = "# distinct point \u2192 measured warm solve\nx = 1\n"
        assert cs.analyze(src, "a.py", None) == []

    def test_check_mark_is_emoji(self):
        f = cs.analyze("x = 1  # done \u2705\n", "a.py", None)
        assert [i.reason for i in f] == ["emoji"]


class TestNarrativePrecision:
    def test_first_person_explanation_is_not_narrative(self):
        src = (
            "# We own the materialised temp dir when using a custom transport.\nown()\n"
        )
        assert cs.analyze(src, "a.py", None) == []

    def test_sequencing_marker_is_narrative(self):
        f = cs.analyze("# Then we flush the queue\nflush()\n", "a.py", None)
        assert [i.reason for i in f] == ["narrative"]

    def test_short_conditional_action_is_narration(self):
        src = "# If the pathname changes, close the menu\nif open:\n    on_close()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["conditional-narration"]

    def test_conditional_action_with_a_reason_is_kept(self):
        src = (
            "# If the sentinel appears, return early to avoid corrupting the batch\n"
            "if row.ts == EPOCH:\n    return\n"
        )
        assert cs.analyze(src, "a.py", None) == []

    def test_conditional_annotation_with_specifics_is_kept(self):
        src = (
            "# If the cache is cold, use the durable path (shared by retries)\n"
            "if not cache:\n    load_durable()\n"
        )
        assert cs.analyze(src, "a.py", None) == []


class TestTargetResolution:
    def test_target_skips_comment_lines_to_reach_code(self):
        """A delimiter between prose and code must not become the target."""
        src = "# sort items\n# " + "-" * 20 + "\nitems.sort()\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f] == [
            (1, "restates-code"),
            (2, "banner"),
        ]


class TestLateTiers:
    def test_file_line_reference_is_flagged(self):
        src = "# mirrors the check in resource_plan.py:412\nguard()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["code-reference"]

    def test_rust_file_line_reference_is_flagged(self):
        src = "# mirrors the check in src/lib.rs:42\nguard()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["code-reference"]

    def test_line_range_reference_is_flagged(self):
        src = "# same as run_simulation.py lines 46-49\nrun()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["code-reference"]

    def test_module_name_without_line_number_is_not_a_reference(self):
        src = "# the authority for this lives in resource_plan.py\nguard()\n"
        assert cs.analyze(src, "a.py", None) == []

    def test_task_number_is_process_leak(self):
        src = "# Task 3: wire the reservation through\nwire()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["process-leak"]

    def test_in_this_pr_is_process_leak(self):
        src = "# left unhandled in this PR; the caller still validates\nskip()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["process-leak"]

    def test_a_quoted_step_is_the_data_not_a_plan(self):
        for body in [
            'Parent keeps its own unprefixed "Step 0: Step_H"',
            'the parser raises "step 10 has no termination" here',
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_a_step_outside_the_quotes_still_leaks(self):
        body = 'rename "step 1" in the YAML during phase 3'
        f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
        assert [i.reason for i in f] == ["process-leak"]
        # The cut takes the plan's phase and leaves the quoted step.
        assert cs.drop_matches(cs.PROCESS_LEAK, cs.unquoted)(body) == (
            "phase 3",
            'rename "step 1" in the YAML during',
        )

    @pytest.mark.parametrize(
        "body",
        [
            'sized in this PR so the 3" pipe matches',
            'the 3" pipe was sized in this PR',
            'the 3" and 4" pipes were sized in this PR',
        ],
    )
    def test_an_unpaired_quote_does_not_hide_a_leak(self, body):
        f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
        assert [i.reason for i in f] == ["process-leak"]

    def test_previously_is_historical(self):
        src = "# an optimizer with no population_size previously skipped this\nsize()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["historical"]

    def test_date_is_historical(self):
        src = "# broken on prod 2026-07-10 when the head node changed\nfix()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["historical"]

    def test_a_date_in_a_format_example_is_not_history(self):
        for body in [
            "@output 2024-05-28T05:55:31+00:00",
            "rendered as absolute UTC, e.g. 2026-07-27 14:30",
            'a range filter such as {"created_at": "gte.2024-01-01"}',
            "reads the plans under dev-docs/plans/2026-09-26-relabel",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_a_pronoun_subject_makes_used_to_historical(self):
        for body in [
            "this used to return a bare array",
            "they used to fall off the end of the loop",
            "three of them used to be emitted here",
        ]:
            f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
            assert [i.reason for i in f] == ["historical"], body

    def test_the_passive_used_to_is_the_contract_not_history(self):
        """50 of 63 real `used to` firings read this way."""
        for body in [
            "the sparsity pattern used to create this coloring",
            "the value is also used to size the worker pool",
            "a name that can be used to target this queue",
            "reference values used to generate the data",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_previously_modifying_a_participle_is_not_history(self):
        """Runtime order, not source history: `previously returned by`."""
        for body in [
            "free a host array previously returned by this library",
            "y should have been previously initialised using the output",
            "an unedited, previously-picked template can be reused",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_a_data_participle_names_the_data_not_its_history(self):
        """A participle naming data the code reads is not a claim about past code."""
        for body in [
            "fit against a previously measured OCV curve",
            "load previously fitted parameters from the last run",
            "cases originally labelled keep score as keep",
            "the file as originally uploaded, when one exists",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_a_data_verb_with_an_object_is_still_history(self):
        f = cs.analyze("# this previously saved the file twice\nrun()\n", "a.py", None)
        assert [i.reason for i in f] == ["historical"]

    def test_a_data_verb_after_a_subject_is_history(self):
        """A bare plural reads as data after the adverb, but not with a subject."""
        for body in [
            "This previously saved files to local storage",
            "it previously recorded timestamps in local time",
            "Batches are small. That originally sampled rows at random",
        ]:
            f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
            assert [i.reason for i in f] == ["historical"], body

    def test_this_mid_sentence_is_a_determiner_over_data(self):
        for body in [
            "load this previously fitted model from the last run",
            "fit against that previously measured OCV curve",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_a_data_participle_opening_the_comment_is_not_edit_narration(self):
        """`edit-narration` also reads the adverb opening a comment, and runs first."""
        for body in [
            "Previously measured OCV values are loaded for the fit",
            "Previously fitted parameters seed the next run",
            "Previously-computed offsets are cached per cell",
            "Previously returned by the parser, the offsets seed this fit",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_a_leading_data_verb_with_an_object_is_still_edit_narration(self):
        f = cs.analyze("# Previously saved the file twice\nrun()\n", "a.py", None)
        assert [i.reason for i in f] == ["edit-narration"]

    def test_first_person_edit_narration_is_unbuilt(self):
        """Code that never reached main: the branch cannot be looked up."""
        for body in [
            "we removed the retry wrapper here",
            "we deleted the old adapter",
            "we also reverted the eager prefetch",
            "we refactored this out of the service layer",
            "we backed out the second index",
        ]:
            f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
            assert [i.reason for i in f] == ["unbuilt"], body

    def test_a_road_not_taken_is_unbuilt(self):
        for body in [
            "we considered adding a debounce but it was not needed",
            "tried a shared cache here but it did not help",
            "prototyped a batch path but abandoned it",
        ]:
            f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
            assert [i.reason for i in f] == ["unbuilt"], body

    def test_absence_describing_the_present_is_not_unbuilt(self):
        """The phrases a broad rule would take, measured across 103k blocks.

        Every one is a present-state fact or a runtime condition, which is why
        `no longer` and `is gone` are absent from the tier -- see
        docs/calibration.md.
        """
        for body in [
            "the deleted row can no longer satisfy the activity log's FK",
            "no row means the channel is gone",
            "rows are removed by the cascade, so no cleanup is needed",
            "unwatching something you no longer watch is still success",
            "entries are dropped when the TTL expires",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_first_person_runtime_narration_is_not_unbuilt(self):
        """`we tried to drop all levels` (pandas) describes what the code does."""
        for body in [
            "we tried to drop all levels, i.e. len(level) == self.nlevels",
            "we replaced the placeholder with the resolved path",
            "we keep the guard because the abort path clears it",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_an_edit_that_is_a_premise_is_not_unbuilt(self):
        """The one firing across both corpora, and it reads the other way.

        pandas' generate_legacy_storage_files: the rename is the reason the
        script branches, so deleting it deletes the argument.
        """
        body = (
            "if we renamed offset to freq in Timestamp, then we have to "
            "conditionally execute because the older signature differs"
        )
        f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
        assert [i.reason for i in f] != ["unbuilt"], body

    def test_over_threshold_is_verbose(self):
        body = " ".join(f"word{i}" for i in range(45))
        f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
        assert [i.reason for i in f] == ["verbose"]

    def test_under_threshold_is_not_verbose(self):
        body = " ".join(f"word{i}" for i in range(30))
        assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == []

    def test_attribute_doc_gets_the_plain_budget(self):
        """58 words over 5 prose lines would clear a 60-word / 7-line ceiling
        for `#:`. Under the plain budget it fires on both."""
        src = (
            "#: Optimizer methods whose scipy entry point holds a *module-global* lock\n"
            "#: for the whole solve, so N threaded starts of it run strictly one at a\n"
            "#: time. Extend as such methods are found; a method absent here is assumed\n"
            "#: to thread freely, which is the conservative assumption because it keeps\n"
            "#: the fit on the point-level path.\n"
            '_PROCESS_LOCKED_METHODS = frozenset({"cobyqa"})\n'
        )
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f if i.reason != "density"] == ["verbose"]

    def test_a_premise_falls_through_to_the_length_fallback(self):
        """Both tiers reach this block and `verbose` is the one that should win.

        It reads as a benchmark -- `measured 2ms` -- but the numbers ARE the
        argument for the threshold, and `would` puts it past the premise guard.
        "Move the numbers to the PR" would delete the reasoning; "shorten it" is
        the right advice for seven lines. `benchmark` is the wrong half of the
        collision recorded in docs/calibration.md."""
        src = (
            "#: Floor for a measured solve time. Not noise suppression: the probe times\n"
            "#: the solve alone, while a task also pays dispatch and serde, and for a\n"
            "#: cheap algebraic fit that overhead exceeds the solve. A production job\n"
            "#: measured 2ms, computed 4.15ms and cost 9.30ms of wall per task, so this\n"
            "#: floor is what makes the wall model land, and lowering it would break it.\n"
            "#: Recalibrate against realized per-task wall from the completion logs,\n"
            "#: never against the warm-solve probe alone.\n"
            "MIN_SOLVE_TIME = 0.01\n"
        )
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f if i.reason != "density"] == ["verbose"]

    def test_a_comparative_gain_is_a_benchmark(self):
        for body in [
            "~144x fewer buffers than the previous encoder",
            "payloads came out 98.2% smaller",
            "40% faster on the stage replica",
            "2.5× cheaper per request",
            "faster by 40%",
        ]:
            f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
            assert [i.reason for i in f] == ["benchmark"], body

    def test_a_measured_duration_is_a_benchmark(self):
        for body in [
            "measured 812ms p99 on stage",
            "costs ~70s a batch instead of ~1.2s for no gain",
            "took 1.2s on the 4.3M-row table",
            "the sweep runs in 340 ms warm",
            "benchmarked at 4.2MB per payload",
        ]:
            f = cs.analyze(f"# {body}\nrun()\n", "a.py", None)
            assert [i.reason for i in f] == ["benchmark"], body

    def test_a_number_that_is_not_a_measurement_is_kept(self):
        """The unit discriminates, not the digit. A limit, a unit, a
        non-latency percentile and a domain ratio all stay silent -- the last
        is corpus.txt:761, the only shape of its kind in the corpora."""
        for body in [
            "Cache duration in milliseconds (5 minutes)",
            "PostgREST caps a range request at 1000 rows",
            "raw words; numpy p98=38, pandas p98=35",
            "Hand back a fit with capacity ~2x the coulomb-count estimate",
            "the header costs 2 bytes and is never omitted",
            "runs on m8i.2xlarge in the spot pool",
            "takes 2 positional args",
        ]:
            assert cs.analyze(f"# {body}\nrun()\n", "a.py", None) == [], body

    def test_no_over_technical_tier_exists(self):
        """Identifier density is not a tier: measured as noise (AUC 0.473-0.515)."""
        src = (
            "# keyed by (job_id, organization_id, objective_name, plot_index)\n"
            "cache = {}\n"
        )
        assert cs.analyze(src, "a.py", None) == []


class TestDensity:
    def test_dense_region_is_flagged(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text(
            "".join(f"# explain {i}\nvalue{i} = compute({i})\n" for i in range(8))
        )
        d = [i for i in cs.scan(str(f)) if i.reason == "density"]
        assert len(d) == 1
        assert "50%" in d[0].text

    def test_sparse_region_is_not_flagged(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text(
            "# one real explanation of the tricky part\n"
            + "".join(f"value{i} = compute({i})\n" for i in range(19))
        )
        assert [i for i in cs.scan(str(f)) if i.reason == "density"] == []

    def test_one_or_two_comment_lines_are_exempt(self, tmp_path):
        """The floor is on comment volume, not region size."""
        f = tmp_path / "a.py"
        f.write_text("# a\nx = 1\n# b\ny = 2\n")
        assert [i for i in cs.scan(str(f)) if i.reason == "density"] == []

    def test_tall_block_in_a_small_change_is_flagged(self, tmp_path):
        """A 5-line comment on a 9-line change is the worst case, not an
        exemption. A region-size floor would let this through."""
        f = tmp_path / "a.py"
        f.write_text(
            "# the upstream feed emits sentinel rows whose timestamps are\n"
            "# zeroed rather than absent, so a plain None check misses them\n"
            "# and the parser downstream raises on the epoch value instead\n"
            "# of skipping, which is why this compares against the epoch\n"
            "# directly rather than testing for falsiness\n"
            "for r in rows:\n"
            "    if r.ts == EPOCH:\n"
            "        continue\n"
            "    out.append(r)\n"
        )
        found = cs.scan(str(f))
        assert any(i.reason == "density" for i in found)
        assert any(i.reason == "verbose" for i in found)

    def test_attribute_doc_comments_count_toward_density(self, tmp_path):
        """Density neutrality would exempt a schema file carrying one `#:`
        line per field, which is the shape the ratio exists to catch."""
        f = tmp_path / "a.py"
        f.write_text(
            "".join(f"#: what field {i} means\nfield{i}: int = 0\n" for i in range(8))
        )
        d = [i for i in cs.scan(str(f)) if i.reason == "density"]
        assert len(d) == 1
        assert "50%" in d[0].text

    def test_lint_directives_do_not_count_toward_density(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text(
            "".join(f"# noqa: E501\nvalue{i} = compute({i})\n" for i in range(8))
        )
        assert [i for i in cs.scan(str(f)) if i.reason == "density"] == []


class TestDensityOnCommentOnlyDiffs:
    """A diff carrying no code has no denominator: 100% is the only value the
    ratio can take, so it is measured over the file instead."""

    def counted(self, src, path="a.py"):
        return cs.density_sets(cs.extract(src, path))

    def test_comment_only_diff_on_a_normal_file_is_not_flagged(self):
        src = (
            "# why the retry budget is per-host and not global\n"
            "# why the deadline is checked before the first attempt\n"
            "# why a 429 does not consume an attempt\n"
            + "".join(f"value{i} = compute({i})\n" for i in range(30))
        )
        counted, neutral = self.counted(src)
        assert cs.density_finding(src, "a.py", {1, 2, 3}, counted, neutral) is None

    def test_comment_only_diff_on_a_comment_dominated_file_still_fires(self):
        src = (
            "# why the retry budget is per-host and not global\n"
            "# why the deadline is checked before the first attempt\n"
            "# why a 429 does not consume an attempt\n"
            + "".join(f"# narration line {i}\n" for i in range(10))
            + "".join(f"value{i} = compute({i})\n" for i in range(4))
        )
        counted, neutral = self.counted(src)
        d = cs.density_finding(src, "a.py", {1, 2, 3}, counted, neutral)
        assert d is not None
        assert "13 of 17 lines in this file" in d.text, d.text
        assert d.line == 1, "anchors to the edit, not to line 1"

    def test_the_comment_floor_applies_before_the_fallback(self):
        """Two changed comment lines are exempt without the file being
        measured -- the file here is 100% comments and must stay silent."""
        src = "".join(f"# narration line {i}\n" for i in range(10))
        counted, neutral = self.counted(src)
        assert cs.density_finding(src, "a.py", {1, 2}, counted, neutral) is None

    def test_whole_file_scan_does_not_double_measure(self):
        """`added is None` is already file-scoped; the fallback must not turn a
        comment-only file into a second, differently-worded finding."""
        src = "".join(f"# narration line {i}\n" for i in range(10))
        counted, neutral = self.counted(src)
        d = cs.density_finding(src, "a.py", None, counted, neutral)
        assert d is not None
        assert "changed lines" in d.text, d.text

    def test_docs_pass_on_a_tracked_file_reports_nothing(self, tmp_path):
        """End to end: a documentation pass is a run of comments-only edits,
        and without the file fallback each one reads as 100% comments."""
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text("".join(f"value{i} = compute({i})\n" for i in range(30)))
        commit_all(repo)
        f.write_text(
            "# why the retry budget is per-host and not global\n"
            "# why the deadline is checked before the first attempt\n"
            "# why a 429 does not consume an attempt\n" + f.read_text()
        )
        assert [i for i in cs.scan(str(f)) if i.reason == "density"] == []


class TestVerboseSpan:
    def test_five_line_block_is_verbose_even_when_short(self):
        """Only 40% of numpy's 5-line blocks exceed VERBOSE_WORDS, so the word
        count alone misses a tall block made of short lines."""
        src = "# alpha\n# beta\n# gamma\n# delta\n# epsilon\nrun()\n"
        f = cs.analyze(src, "a.py", None)
        assert [(i.line, i.reason) for i in f if i.reason != "density"] == [
            (1, "verbose")
        ]

    def test_two_line_block_is_not_verbose_on_span_alone(self):
        """VERBOSE_LINES is 3, set on human labels: no comment the author kept
        runs past two lines, so two stays acceptable."""
        src = "# alpha\n# beta\nrun()\n"
        assert cs.analyze(src, "a.py", None) == []

    def test_three_line_block_is_verbose(self):
        src = "# alpha\n# beta\n# gamma\nrun()\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f if i.reason != "density"] == ["verbose"]


class TestNarrationIsNotADeterministicTier:
    """Narration needs a curated verb list to detect deterministically, and
    curation is the problem: naming which verbs count needs endless growing,
    and a POS tagger cannot substitute -- taggers read "Configure" and "Loop"
    as nouns in comment-initial position, and imperative mood does not separate
    the classes ("Guard against sentinel rows the upstream feed emits" is
    imperative and worth keeping). It is the opt-in --llm sweep's job instead.
    """

    def test_hook_path_does_not_flag_narration(self):
        """Deliberate gap. The aggregate still shows up via `density`."""
        src = "# Loop over every row\nfor r in rows:\n    pass\n"
        assert cs.analyze(src, "a.py", None) == []


class TestFailureIsAlwaysSilent:
    """The hook must never break the edit loop. These cover the guarantee
    rather than the happy path, which never reaches these lines."""

    def test_git_missing_treats_every_line_as_new(self, tmp_path, monkeypatch):
        def no_git(*a, **k):
            raise FileNotFoundError("git")

        monkeypatch.setattr(cs.subprocess, "run", no_git)
        f = tmp_path / "a.py"
        f.write_text("# ===== X =====\nx = 1\n")
        assert cs.added_lines(str(f)) is None
        assert [i.reason for i in cs.scan(str(f))] == ["banner"]

    def test_git_timeout_treats_every_line_as_new(self, tmp_path, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=10)

        monkeypatch.setattr(cs.subprocess, "run", slow)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert cs.added_lines(str(f)) is None

    def test_git_nonzero_exit_treats_every_line_as_new(self, tmp_path, monkeypatch):
        real = cs._git

        def failing(args, cwd):
            return (0, "") if args[0] == "ls-files" else (128, "")

        monkeypatch.setattr(cs, "_git", failing)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert cs.added_lines(str(f)) is None
        monkeypatch.setattr(cs, "_git", real)

    def test_path_whose_parent_is_not_a_directory(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert cs.added_lines(str(f / "nested.py")) is None

    def test_unforeseen_failure_in_scan_never_propagates(
        self, tmp_path, capsys, monkeypatch
    ):
        """The outermost guard: a bug in scan must not break the edit."""

        def boom(*a, **k):
            raise RuntimeError("unforeseen")

        monkeypatch.setattr(cs, "scan", boom)
        f = tmp_path / "a.py"
        f.write_text("# ===== X =====\n")
        assert cs.main([str(f)]) == 0
        assert capsys.readouterr().err == ""

    def test_one_bad_path_does_not_suppress_the_others(
        self, tmp_path, capsys, monkeypatch
    ):
        good = tmp_path / "good.py"
        good.write_text("# ===== FINDME =====\nx = 1\n")
        real = cs.scan

        # Match the basename: pytest derives tmp_path from the test name, so a
        # substring check against the full path also matches the good file.
        def selective(path, *args, **kwargs):
            if os.path.basename(path) == "boom.py":
                raise RuntimeError("unforeseen")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(cs, "scan", selective)
        assert cs.main([str(tmp_path / "boom.py"), str(good)]) == 2
        assert "FINDME" in capsys.readouterr().err


class TestJsStringEscapes:
    def test_escaped_quote_does_not_end_the_string(self):
        src = 'const s = "a \\" // not a comment";\nconst x = 1; // here\n'
        assert cs.js_comments(src) == [cs.Comment(2, 13, ("here",))]

    def test_escaped_backslash_before_quote(self):
        src = 'const s = "a \\\\";\nconst x = 1; // here\n'
        assert cs.js_comments(src) == [cs.Comment(2, 13, ("here",))]


class TestDensityThresholdItself:
    def test_enough_comments_to_pass_the_floor_but_acceptable_density(self, tmp_path):
        """Distinct from the floor case: 3 comment lines clears
        DENSITY_MIN_COMMENT_LINES, so this exercises the ratio comparison."""
        f = tmp_path / "a.py"
        f.write_text(
            "# why the first branch is ordered before the second\n"
            "a = step_one()\nb = step_two()\nc = step_three()\nd = step_four()\n"
            "# why this guard cannot be hoisted out of the loop\n"
            "e = step_five()\nf = step_six()\ng = step_seven()\nh = step_eight()\n"
            "# why the result is not cached across calls\n"
            "i = step_nine()\nj = step_ten()\nk = step_eleven()\n"
        )
        d = [x for x in cs.scan(str(f)) if x.reason == "density"]
        assert d == [], "3 of 14 lines is 21%, under DENSITY_MAX"

    def test_just_over_the_threshold_fires(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text(
            "# why the first branch is ordered before the second\n"
            "# why this guard cannot be hoisted out of the loop\n"
            "# why the result is not cached across calls\n"
            "# why the fallback path is not shared with the primary\n"
            "a = step_one()\nb = step_two()\nc = step_three()\nd = step_four()\n"
            "e = step_five()\nf = step_six()\n"
        )
        d = [x for x in cs.scan(str(f)) if x.reason == "density"]
        assert len(d) == 1, "4 of 10 lines is 40%, over DENSITY_MAX"
        assert "40%" in d[0].text


class TestDisplayPath:
    def test_unrelatable_path_is_shown_as_given(self, monkeypatch):
        """os.path.relpath raises across Windows drives; show the path as-is."""

        def cross_drive(*a, **k):
            raise ValueError("path is on mount 'C:', start on mount 'D:'")

        monkeypatch.setattr(cs.os.path, "relpath", cross_drive)
        assert cs.report(
            "D:\\x\\a.py", [cs.Finding(1, "banner", "===", cs.DELETE)]
        ).startswith("comment-slop: D:\\x\\a.py")


class TestHumanLabelledFixes:
    """Gaps found by scoring against 116 human labels (see eval/)."""

    def test_delimiter_wrapped_label_with_punctuation(self):
        """`[\\w\\s]`-only would miss "--- Tests for env-var-configurable defaults ---"."""
        src = "# --- Tests for env-var-configurable defaults ---\nclass T:\n    pass\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["banner"]

    def test_test_numbering_is_a_step_marker(self):
        src = "# Test 2: Filter with x_max only\nassert f(x) == y\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["step-marker"]

    def test_bare_identifier_is_flagged(self):
        """When the identifier also appears in the code, `restates-code` wins --
        it is the more precise reading. This covers the case where it does not."""
        src = "# EISStepForm\nreturn render(props)\n"
        f = cs.analyze(src, "a.py", None)
        assert [i.reason for i in f] == ["bare-identifier"]

    def test_identifier_matching_its_code_reads_as_restatement(self):
        f = cs.analyze("# EISStepForm\ndef EISStepForm():\n    pass\n", "a.py", None)
        assert [i.reason for i in f] == ["restates-code"]

    def test_known_section_word_still_reads_as_section_label(self):
        """ "Helpers" is both a bare identifier and a section label; the more
        specific tier must win."""
        f = cs.analyze("# Helpers\ndef helper():\n    pass\n", "a.py", None)
        assert [i.reason for i in f] == ["section-label"]

    def test_a_sentence_is_not_a_bare_identifier(self):
        assert (
            cs.analyze("# guard the empty case\nif not xs:\n    return\n", "a.py", None)
            == []
        )


def head_sha(repo: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


class TestSinceRef:
    """`--since REF` is the gate's CI mode.

    Every tracked file matches HEAD in a clean checkout, so the default
    comparison reports nothing there -- and a hook that passes for that reason
    is worse than no hook, because the green check reads as coverage.
    """

    def committed_slop(self, tmp_path: Path) -> tuple[Path, str]:
        """A repo whose newest commit adds a banner, plus the SHA before it."""
        repo = make_repo(tmp_path)
        (repo / "old.py").write_text("x = 1\n")
        commit_all(repo, "base")
        base = head_sha(repo)
        (repo / "new.py").write_text("y = 2\n# ===== TOTALS =====\nz = 3\n")
        commit_all(repo, "work")
        return repo, base

    def test_committed_change_is_reported_against_the_ref(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, base = self.committed_slop(tmp_path)
        monkeypatch.chdir(repo)
        assert cs.main(["--since", base, "new.py"]) == 2
        assert "[banner]" in capsys.readouterr().err

    def test_default_head_comparison_reports_nothing_on_a_clean_tree(
        self, tmp_path, monkeypatch
    ):
        # The trap the flag exists for: same file, same slop, clean checkout.
        repo, _ = self.committed_slop(tmp_path)
        monkeypatch.chdir(repo)
        assert cs.main(["new.py"]) == 0

    def test_only_changed_files_appear_in_the_report(
        self, tmp_path, capsys, monkeypatch
    ):
        repo, _ = self.committed_slop(tmp_path)
        (repo / "stale.py").write_text("# ===== OLD =====\nx = 1\n")
        commit_all(repo, "stale")
        before = head_sha(repo)
        (repo / "new.py").write_text(
            "y = 2\n# ===== TOTALS =====\nz = 3\n# Fixed: it\nw = 4\n"
        )
        commit_all(repo, "more work")
        monkeypatch.chdir(repo)
        assert cs.main(["--since", before, "old.py", "stale.py", "new.py"]) == 2
        err = capsys.readouterr().err
        assert "new.py" in err
        assert "stale.py" not in err

    def test_unresolvable_ref_scans_nothing(self, tmp_path, monkeypatch):
        repo, _ = self.committed_slop(tmp_path)
        monkeypatch.chdir(repo)
        # Silent only because scripts/check-comment-slop.sh resolves the ref
        # itself and fails the job when it cannot.
        assert cs.main(["--since", "no-such-ref", "new.py"]) == 0

    def test_added_lines_accepts_a_ref(self, tmp_path):
        repo, base = self.committed_slop(tmp_path)
        assert cs.added_lines(str(repo / "new.py"), base) == {1, 2, 3}
        assert cs.added_lines(str(repo / "new.py")) == set()

    def test_changed_files_lists_paths_the_ref_changed(self, tmp_path):
        repo, base = self.committed_slop(tmp_path)
        assert cs.changed_files(base, str(repo)) == {str(repo / "new.py")}
        assert cs.changed_files("no-such-ref", str(repo)) is None

    def test_changed_files_counts_untracked_as_changed(self, tmp_path):
        repo, base = self.committed_slop(tmp_path)
        (repo / "loose.py").write_text("x = 1\n")
        assert cs.changed_files(base, str(repo)) == {
            str(repo / "new.py"),
            str(repo / "loose.py"),
        }


class TestNarrowing:
    """Unchanged files are dropped before scanning, not scanned and discarded.

    `added_lines` costs a `git diff` per path, and prek hands the gate every file
    it is configured for -- the whole repository under `--all-files`.
    """

    def two_files_one_changed(self, tmp_path: Path) -> Path:
        tmp_path.mkdir(parents=True, exist_ok=True)
        repo = make_repo(tmp_path)
        (repo / "quiet.py").write_text("# ===== OLD =====\nx = 1\n")
        (repo / "touched.py").write_text("y = 2\n")
        commit_all(repo)
        (repo / "touched.py").write_text("y = 2\n# ===== TOTALS =====\nz = 3\n")
        return repo

    def scanned(self, repo: Path, argv: list[str], monkeypatch) -> list[str]:
        seen = []
        real = cs.scan
        monkeypatch.setattr(
            cs, "scan", lambda p, *a, **k: seen.append(p) or real(p, *a, **k)
        )
        monkeypatch.chdir(repo)
        cs.main(argv)
        return [os.path.basename(p) for p in seen]

    def test_unchanged_file_is_never_scanned(self, tmp_path, monkeypatch):
        repo = self.two_files_one_changed(tmp_path)
        assert self.scanned(repo, ["quiet.py", "touched.py"], monkeypatch) == [
            "touched.py"
        ]

    def test_all_flag_scans_everything_it_is_given(self, tmp_path, monkeypatch):
        repo = self.two_files_one_changed(tmp_path)
        # `--all` is the deliberate sweep of committed code; narrowing it would
        # make the documented sweep report nothing.
        assert sorted(
            self.scanned(repo, ["--all", "quiet.py", "touched.py"], monkeypatch)
        ) == ["quiet.py", "touched.py"]

    def test_untracked_file_survives_narrowing(self, tmp_path, monkeypatch):
        repo = self.two_files_one_changed(tmp_path)
        (repo / "loose.py").write_text("# ===== NEW =====\nx = 1\n")
        assert "loose.py" in self.scanned(repo, ["quiet.py", "loose.py"], monkeypatch)

    def test_outside_a_repo_nothing_is_narrowed(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("# ===== X =====\nx = 1\n")
        assert self.scanned(tmp_path, ["a.py"], monkeypatch) == ["a.py"]

    def test_another_repos_answer_does_not_narrow_this_one(self, tmp_path, monkeypatch):
        # Keyed on the process cwd instead of per repository, a path in another
        # checkout is "unchanged" there and silently dropped.
        here = self.two_files_one_changed(tmp_path / "here")
        elsewhere = self.two_files_one_changed(tmp_path / "elsewhere")
        monkeypatch.chdir(here)
        seen = []
        real = cs.scan
        monkeypatch.setattr(
            cs, "scan", lambda p, *a, **k: seen.append(p) or real(p, *a, **k)
        )
        cs.main([str(here / "touched.py"), str(elsewhere / "touched.py")])
        assert len(seen) == 2


class TestBenchmarkSparesNumbersThatCarryAnArgument:
    """A cost that justifies a decision is a premise, not a snapshot: deleting
    the number deletes the reason. Both of the tier's hits on this hook's own
    source are of that shape."""

    @pytest.mark.parametrize(
        "body",
        [
            "Costs 1.3 ms/file, because dropping findings on a syntax error "
            "would be a regression",
            "Sampling costs 40 ms per file, which is why it is off by default",
            "This takes 30 minutes on a cold cache, so it runs nightly",
        ],
    )
    def test_a_premise_is_kept(self, body):
        assert not cs.benchmark(body)

    @pytest.mark.parametrize(
        "body",
        [
            "measured 812ms on an M2",
            "~144x fewer buffers",
            "98.2% smaller",
            "measured baseline: ~138 MB",
        ],
    )
    def test_a_snapshot_still_fires(self, body):
        assert cs.benchmark(body)

    def test_the_hook_does_not_flag_itself(self):
        src = (SRC / "comment_slop.py").read_text(encoding="utf-8")
        got = cs.analyze(src, "comment_slop.py")
        assert [f for f in got if f.reason == "benchmark"] == []


class TestTheManualSweepActuallySweeps:
    def test_a_quoted_glob_is_expanded(self, tmp_path):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "a.py").write_text("# Fixed: a\n", encoding="utf-8")
        (tmp_path / "pkg" / "b.py").write_text("# Fixed: b\n", encoding="utf-8")
        got = cs.expand([str(tmp_path / "**" / "*.py")])
        assert [os.path.basename(p) for p in got] == ["a.py", "b.py"]

    def test_a_directory_is_walked(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "src" / "notes.md").write_text("hi\n", encoding="utf-8")
        assert [os.path.basename(p) for p in cs.expand([str(tmp_path)])] == ["a.py"]

    def test_build_output_is_not_swept(self, tmp_path):
        for d in ("src", "node_modules"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "a.ts").write_text("// x\n", encoding="utf-8")
        got = cs.expand([str(tmp_path)])
        assert len(got) == 1 and "node_modules" not in got[0]

    def test_a_literal_pattern_no_longer_scans_nothing(self, tmp_path):
        """The README's own example reached `os.path.getsize` as a filename."""
        (tmp_path / "a.py").write_text("# Fixed: a\n", encoding="utf-8")
        assert cs.expand([str(tmp_path / "*.py")])

    def test_all_reports_committed_lines(self, tmp_path):
        """`--include-unreviewed` is a deliberate sweep, but diff scoping alone
        would have a clean tracked file report nothing at all."""
        path = tmp_path / "a.py"
        path.write_text("# Fixed: use the cache\nx = 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "a.py"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
            cwd=tmp_path,
            check=True,
        )
        assert cs.scan(str(path)) == []
        assert [f.reason for f in cs.scan(str(path), whole_file=True)] == [
            "edit-narration"
        ]
