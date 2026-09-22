"""Span-aware scoping and context resolution.

A comment that spans lines is scoped by its whole range and explained by the
code after its LAST line. Keying either on the first line means editing deep
inside a docstring scans nothing, and hands the model the comment's own tail as
"the code it describes".
"""

from pathlib import Path
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def commit_all(repo: Path, message: str = "wip") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


JSDOC = (
    "function f() {\n"
    "  /**\n"
    "   * Fixed: skip None entries when the pack is empty\n"
    "   * and also when it is not\n"
    "   */\n"
    "  return 1;\n"
    "}\n"
)


def with_raw(raw):
    """find_findings driven from synthetic Comments."""

    def run(src, added, path="a.ts"):
        return cs.find_findings(src, path, added, False, raw)

    return run


class TestAddedLinesIntersectTheWholeBlock:
    def test_editing_an_interior_line_scans_the_block(self):
        raw = [
            cs.Comment(
                2,
                2,
                (
                    "",
                    "Fixed: skip None entries when the pack is empty",
                    "and also when it is not",
                    "",
                ),
                cs.DOC,
                mergeable=False,
            )
        ]
        f = with_raw(raw)(JSDOC, {4})
        assert [i.reason for i in f] == ["edit-narration"]

    def test_editing_the_opening_line_still_scans_the_block(self):
        raw = [
            cs.Comment(
                2, 2, ("", "Fixed: skip None entries", ""), cs.DOC, mergeable=False
            )
        ]
        assert with_raw(raw)(JSDOC, {2}) != []

    def test_a_block_outside_the_diff_is_skipped(self):
        raw = [
            cs.Comment(
                2, 2, ("", "Fixed: skip None entries", ""), cs.DOC, mergeable=False
            )
        ]
        assert with_raw(raw)(JSDOC, {7}) == []

    def test_no_diff_information_scans_everything(self):
        raw = [
            cs.Comment(
                2, 2, ("", "Fixed: skip None entries", ""), cs.DOC, mergeable=False
            )
        ]
        assert with_raw(raw)(JSDOC, None) != []


class TestContextResolvesAfterTheBlock:
    def test_context_is_the_code_below_not_the_comment_itself(self):
        lines = JSDOC.split("\n")
        b = cs.Block(2, 2, ("", "one", "two", ""), cs.DOC, False)
        assert cs.block_context(lines, b, 4) == ["return 1;", "}"]

    def test_context_for_a_single_line_comment_is_unchanged(self):
        lines = ["# note", "x = 1", "y = 2"]
        b = cs.Block(1, 0, ("note",), cs.PLAIN, True)
        assert cs.block_context(lines, b, 4) == ["x = 1", "y = 2"]

    def test_restates_code_compares_against_the_line_below_the_block(self):
        src = "/**\n * render props\n */\nreturn render(props);\n"
        raw = [cs.Comment(1, 0, ("", "render props", ""), cs.PLAIN, mergeable=False)]
        f = cs.find_findings(src, "a.ts", None, False, raw)
        assert [i.reason for i in f] == ["restates-code"]


class TestSweepPreFilterIsSpanAware:
    """scan()'s --llm sweep has its own added-line pre-filter, separate from
    find_findings'. It keyed on the block's opening line only, so editing an
    interior line of a grouped comment silently dropped it from the model
    batch -- the same bug find_findings had, in a fourth place.
    """

    def test_editing_an_interior_line_still_reaches_the_sweep(
        self, tmp_path, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            cs,
            "classify",
            lambda pairs, deadline=None: (
                seen.extend(pairs) or {ln for ln, _, _ in pairs}
            ),
        )
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text(
            "# Loop over every row\n# doing the work\nfor r in rows:\n    pass\n"
        )
        commit_all(repo)
        f.write_text(
            "# Loop over every row\n# without exception\nfor r in rows:\n    pass\n"
        )
        assert cs.added_lines(str(f)) == {2}

        found = cs.scan(str(f), use_llm=True)
        assert [(i.line, i.reason) for i in found] == [(1, "narration (model)")]
        assert seen


LONG = " ".join(f"word{i}" for i in range(45))
DOC_LONG = " ".join(f"word{i}" for i in range(91))


def doc(lines, line=1, col=0):
    return cs.Comment(line, col, tuple(lines), cs.DOC, mergeable=False)


def findings(raw, src, path="a.ts"):
    return cs.find_findings(src, path, None, False, raw)


class TestDocProse:
    def test_narrative_before_a_jsdoc_tag(self):
        assert cs.doc_prose(("Return the run.", "", "@param id - the run id")) == (
            "Return the run.",
            "",
        )

    def test_a_tag_only_block_has_an_empty_prologue(self):
        assert cs.doc_prose(("@param id - the run id",)) == ()

    def test_numpydoc_section_header_needs_its_underline(self):
        assert cs.doc_prose(
            ("Summary.", "", "Parameters", "----------", "x : int")
        ) == ("Summary.", "")
        assert cs.doc_prose(("Not a section: Parameters is inline",)) == (
            "Not a section: Parameters is inline",
        )

    def test_rustdoc_section(self):
        assert cs.doc_prose(
            ("Fetch the pack.", "", "# Examples", "```", "foo()", "```")
        ) == ("Fetch the pack.", "")

    def test_an_untagged_block_is_all_prologue(self):
        assert cs.doc_prose(("Just narrative.", "over two lines.")) == (
            "Just narrative.",
            "over two lines.",
        )

    def test_a_numpydoc_notes_section_is_prose(self):
        assert cs.doc_prose(
            (
                "Summary.",
                "",
                "Parameters",
                "----------",
                "x : int",
                "    the thing",
                "",
                "Notes",
                "-----",
                "The essay.",
                "Over two lines.",
            )
        ) == ("Summary.", "", "The essay.", "Over two lines.")

    def test_prescribed_sections_after_a_prose_one_are_dropped_again(self):
        assert cs.doc_prose(
            ("Notes", "-----", "kept.", "", "Examples", "--------", ">>> f()")
        ) == ("kept.", "")

    def test_numpydoc_warnings_is_prose_but_warns_is_a_list(self):
        assert cs.doc_prose(("Warnings", "--------", "Do not call twice.")) == (
            "Do not call twice.",
        )
        assert cs.doc_prose(("Warns", "-----", "UserWarning")) == ()

    def test_rustdoc_notes_is_prose_but_errors_is_prescribed(self):
        assert cs.doc_prose(("# Notes", "The essay.")) == ("The essay.",)
        assert cs.doc_prose(("# Errors", "Fails when the pack is empty.")) == ()

    def test_a_jsdoc_remarks_tag_carries_prose_on_its_own_line(self):
        assert cs.doc_prose(
            (
                "Summary.",
                "@remarks The essay starts here.",
                "and continues.",
                "@param a - one",
            )
        ) == ("Summary.", "The essay starts here.", "and continues.")


class TestDocClassification:
    def test_a_genuinely_long_untagged_doc_block_is_verbose(self):
        raw = [doc([DOC_LONG])]
        assert [i.reason for i in findings(raw, f"/** {DOC_LONG} */\nrun();\n")] == [
            "verbose"
        ]

    def test_a_ten_line_untagged_doc_block_is_verbose_on_span(self):
        lines = [f"line {i}" for i in range(10)]
        raw = [doc(["", *lines, ""])]
        src = "/**\n" + "".join(f" * {line}\n" for line in lines) + " */\nrun();\n"
        assert [i.reason for i in findings(raw, src)] == ["verbose"]

    def test_retry_docstring_can_explain_bounded_how(self):
        src = (
            "def is_connection_drop(error):\n"
            '    """Return True when the connection died before the server sent a response.\n'
            "\n"
            "    Not reachable via the HTTP error predicate: no status line was ever\n"
            "    received, so the exception has no status code and its message has no\n"
            "    server-error substring to match. Storage drops the TCP connection\n"
            "    under burst load, surfacing as a remote protocol error.\n"
            "\n"
            "    Match on type first because timeout exceptions can carry an empty\n"
            "    message, so substring matching alone would miss them.\n"
            '    """\n'
            "    return check(error)\n"
        )
        assert cs.find_findings(src, "storage_utils.py", None) == []

    def test_doc_budget_allows_ninety_words_or_nine_lines(self):
        at_word_cap = " ".join(f"word{i}" for i in range(90))
        assert findings([doc([at_word_cap])], f"/** {at_word_cap} */\nrun();\n") == []

        lines = [f"detail {i}" for i in range(9)]
        raw = [doc(["", *lines, ""])]
        src = "/**\n" + "".join(f" * {line}\n" for line in lines) + " */\nrun();\n"
        assert findings(raw, src) == []

    def test_structured_length_is_not_verbose(self):
        raw = [
            doc(["Short.", "", "@param a - one", "@param b - two", "@returns " + LONG])
        ]
        src = "/**\n * Short.\n *\n * @param a - one\n */\nrun();\n"
        assert findings(raw, src) == []

    def test_narrative_before_a_tag_is_still_judged(self):
        """One @param used to veto the whole block through NEVER."""
        raw = [doc(["Fixed: skip None entries", "", "@param a - one"])]
        src = "/**\n * Fixed: skip None entries\n *\n * @param a - one\n */\nrun();\n"
        assert [i.reason for i in findings(raw, src)] == ["edit-narration"]

    def test_a_notes_essay_is_judged_with_the_summary(self):
        """The Notes block was exempt with the prescribed sections around it."""
        essay = "\n".join(
            f"    Sentence {i} of the essay, restated at length." for i in range(11)
        )
        src = (
            "def spot_pool_size(types, zones):\n"
            '    """Return the number of spot pools the request can draw from.\n'
            "\n"
            "    Parameters\n"
            "    ----------\n"
            "    types : list[str]\n"
            "        The families the image supports.\n"
            "\n"
            "    Notes\n"
            "    -----\n"
            f"{essay}\n"
            '    """\n'
            "    return len(types) * len(zones)\n"
        )
        assert [i.reason for i in cs.find_findings(src, "fleet.py", None)] == [
            "verbose"
        ]

    def test_a_long_numpydoc_parameter_list_is_still_prescribed(self):
        params = "\n".join(
            f"    arg{i} : int\n        The {i}th knob to turn." for i in range(12)
        )
        src = (
            "def configure(*args):\n"
            '    """Apply the fleet knobs.\n'
            "\n"
            "    Parameters\n"
            "    ----------\n"
            f"{params}\n"
            "\n"
            "    Notes\n"
            "    -----\n"
            "    Order matters: the subnet list is read last.\n"
            '    """\n'
            "    return args\n"
        )
        assert cs.find_findings(src, "fleet.py", None) == []

    def test_defect_tiers_still_apply_to_docs(self):
        for body, want in [
            ("Fixed: skip None entries", "edit-narration"),
            ("this previously accepted a bare dict", "historical"),
            ("Task 3: wire the reservation", "process-leak"),
        ]:
            raw = [doc([body])]
            assert [i.reason for i in findings(raw, f"/** {body} */\nrun();\n")] == [
                want
            ], body

    def test_pr_1430_block_is_within_the_bounded_how_budget(self):
        """The original calibration treated any five-line explanation as
        slop. Dogfood showed this race description is useful API behavior."""
        src = (
            "getOptimization: builder.query({\n"
            "  /**\n"
            "   * The backend guarantees metadata and runs are persisted\n"
            "   * before this endpoint reports succeeded. It cannot guarantee\n"
            "   * anything about a payload read that was already in flight\n"
            "   * when the status flipped, so a pre-terminal response can\n"
            "   * land after the terminal status and stick.\n"
            "   */\n"
            "  onQueryStarted: async (id) => { return id; },\n"
            "});\n"
        )
        f = cs.find_findings(src, "optimizations-api.ts", None)
        assert f == []


class TestDensityIgnoresDocsEntirely:
    def test_doc_lines_are_on_neither_side_of_the_ratio(self):
        """Counting docs as code makes a documented file read as sparse;
        counting them as comment flags it for being documented."""
        src = "\n".join(
            [
                "/// doc one",
                "/// doc two",
                "/// doc three",
                "// real one",
                "// real two",
                "// real three",
                "let a = 1;",
                "let b = 2;",
            ]
        )
        counted = {4, 5, 6}
        neutral = {1, 2, 3}
        d = cs.density_finding(src, "a.rs", None, counted, neutral)
        assert d is not None
        assert "3 of 5" in d.text, d.text


class TestSweepDeadline:
    def test_classify_stops_when_the_deadline_passes(self, monkeypatch):
        """A batch cap bounds calls, not latency. Three batches against a
        120s timeout is a six-minute stall on one edit."""
        calls = []

        def slow_urlopen(req, timeout=None):
            calls.append(req)
            time.sleep(0.2)
            raise RuntimeError("counted, not called for real")

        monkeypatch.setattr(urllib.request, "urlopen", slow_urlopen)
        pairs = [(i, f"comment {i}", ["run()"]) for i in range(1, 60)]
        start = time.monotonic()
        cs.classify(pairs, deadline=0.3)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"deadline not honoured: {elapsed:.1f}s"
        assert len(calls) < 5, calls

    def test_classify_without_a_deadline_covers_every_batch(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise RuntimeError("counted")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        pairs = [(i, f"comment {i}", ["run()"]) for i in range(1, 40)]
        cs.classify(pairs)
        assert len(calls) == 4, "39 pairs at LLM_BATCH=12 is 4 batches"

    def test_truncation_is_reported_not_silent(self, monkeypatch, capsys):
        def instant_fail(req, timeout=None):
            raise RuntimeError("unreachable")

        monkeypatch.setattr(urllib.request, "urlopen", instant_fail)
        pairs = [(i, f"comment {i}", ["run()"]) for i in range(1, 40)]
        cs.classify(pairs, deadline=-1)
        err = capsys.readouterr().err
        assert "not sent" in err or "deadline" in err.lower()
