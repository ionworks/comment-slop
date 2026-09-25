"""The pipeline from `extract` to findings: grouping, scope, context, doc prose,
exemptions and density.

A comment that spans lines is scoped by its whole range and explained by the
code after its LAST line. Keying either on the first line means editing deep
inside a docstring scans nothing, and hands the model the comment's own tail as
"the code it describes".
"""

from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs
import recalibrate as rc


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


class TestAddedLinesIntersectTheWholeBlock:
    def test_editing_an_interior_line_scans_the_block(self):
        assert [i.reason for i in cs.analyze(JSDOC, "a.ts", {4})] == ["edit-narration"]

    def test_editing_the_opening_line_still_scans_the_block(self):
        assert cs.analyze(JSDOC, "a.ts", {2}) != []

    def test_a_block_outside_the_diff_is_skipped(self):
        assert cs.analyze(JSDOC, "a.ts", {7}) == []

    def test_no_diff_information_scans_everything(self):
        assert cs.analyze(JSDOC, "a.ts", None) != []


class TestContextResolvesAfterTheBlock:
    def test_context_is_the_code_below_not_the_comment_itself(self):
        lines = JSDOC.split("\n")
        b = cs.Comment(2, 2, ("", "one", "two", ""), cs.DOC, False)
        assert cs.block_context(lines, b, 4) == ["return 1;", "}"]

    def test_context_for_a_single_line_comment_is_unchanged(self):
        lines = ["# note", "x = 1", "y = 2"]
        b = cs.Comment(1, 0, ("note",), cs.PLAIN, True)
        assert cs.block_context(lines, b, 4) == ["x = 1", "y = 2"]

    def test_restates_code_compares_against_the_line_below_the_block(self):
        src = "/*\n * render props\n */\nreturn render(props);\n"
        found = [i.reason for i in cs.analyze(src, "a.ts")]
        assert [r for r in found if r != "density"] == ["restates-code"]


LONG = " ".join(f"word{i}" for i in range(45))
DOC_LONG = " ".join(f"word{i}" for i in range(91))


def jsdoc(*lines):
    """A JSDoc block holding `lines`, above one line of code."""
    return "/**\n" + "".join(f" * {ln}\n" for ln in lines) + " */\nrun();\n"


def tiers(src, path="a.ts"):
    return [f.reason for f in cs.analyze(src, path)]


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
        assert tiers(f"/** {DOC_LONG} */\nrun();\n") == ["verbose"]

    def test_a_ten_line_untagged_doc_block_is_verbose_on_span(self):
        assert tiers(jsdoc(*(f"line {i}" for i in range(10)))) == ["verbose"]

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
        assert cs.analyze(src, "storage_utils.py", None) == []

    def test_doc_budget_allows_ninety_words_or_nine_lines(self):
        at_word_cap = " ".join(f"word{i}" for i in range(90))
        assert tiers(f"/** {at_word_cap} */\nrun();\n") == []
        assert tiers(jsdoc(*(f"detail {i}" for i in range(9)))) == []

    def test_structured_length_is_not_verbose(self):
        src = jsdoc(
            "Short.", "", "@param a - one", "@param b - two", "@returns " + LONG
        )
        assert tiers(src) == []

    def test_narrative_before_a_tag_is_still_judged(self):
        """Vetoed through NEVER, one @param would silence the whole block."""
        src = jsdoc("Fixed: skip None entries", "", "@param a - one")
        assert tiers(src) == ["edit-narration"]

    def test_a_notes_essay_is_judged_with_the_summary(self):
        """Truncating at the first marker would exempt the Notes block along
        with the prescribed sections around it."""
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
        assert [i.reason for i in cs.analyze(src, "fleet.py", None)] == ["verbose"]

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
        assert cs.analyze(src, "fleet.py", None) == []

    def test_defect_tiers_still_apply_to_docs(self):
        for body, want in [
            ("Fixed: skip None entries", "edit-narration"),
            ("this previously accepted a bare dict", "historical"),
            ("Task 3: wire the reservation", "process-leak"),
        ]:
            assert tiers(f"/** {body} */\nrun();\n") == [want], body

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
        f = cs.analyze(src, "optimizations-api.ts", None)
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


def reasons(src, name):
    """(line, tier) for every finding on `src`, read whole as the file `name`."""
    return [(f.line, f.reason) for f in cs.analyze(src, name)]


class TestAnExemptLineDoesNotSilenceItsNeighbours:
    """`NEVER` is searched against the JOINED body of a grouped block, so an
    exempt line allowed to merge would veto every comment merged with it."""

    @pytest.mark.parametrize(
        "veto",
        [
            "# TODO: revisit the eviction policy",
            "# See https://example.com/spec",
            "# NOTE: the cache is warm by now",
            "# noqa: E501",
            "# FIXME: drop this once the API lands",
        ],
    )
    def test_an_exempt_line_above_a_finding(self, veto):
        src = f"def f():\n    {veto}\n    # Fixed: use the cache\n    return 1\n"
        assert reasons(src, "a.py") == [(3, "edit-narration")]

    def test_an_exempt_line_below_a_finding(self):
        src = (
            "def f():\n    # Fixed: use the cache\n"
            "    # TODO: revisit later\n    return 1\n"
        )
        assert reasons(src, "a.py") == [(2, "edit-narration")]

    def test_a_genuine_wrapped_paragraph_still_merges(self):
        """The merge exists so a continuation is not judged out of context, and
        keeping exempt lines out of it must not stop a real paragraph merging."""
        src = (
            "def f():\n"
            "    # The cache is keyed on the request id rather than the URL,\n"
            "    # because two callers can ask for the same URL in one turn.\n"
            "    return 1\n"
        )
        blocks = cs.group_blocks(cs.extract(src, "a.py"))
        assert [b.span for b in blocks] == [2]
        assert reasons(src, "a.py") == []

    def test_an_exempt_comment_is_still_exempt_on_its_own(self):
        src = "def f():\n    # TODO: revisit the eviction policy\n    return 1\n"
        assert reasons(src, "a.py") == []


class TestOnlyTheShebangIsSkipped:
    def test_rust_inner_doc_on_line_one_is_judged(self):
        """`//!` is crate documentation, not a shebang. Skipped as one, a slop
        comment in a crate's opening block would be missed while the identical
        comment one line down fired."""
        src = "//! Fixed: migrated API\npub fn a() {}\n"
        assert reasons(src, "a.rs") == [(1, "edit-narration")]

    def test_the_same_comment_lower_down_still_fires(self):
        src = "pub fn z() {}\n//! Fixed: migrated API\npub fn a() {}\n"
        assert reasons(src, "a.rs") == [(2, "edit-narration")]

    def test_a_comment_after_a_rust_attribute_on_line_one_is_judged(self):
        """`#![...]` starts with `#!` but is an attribute; only a comment at
        column 0 of line 1 is a shebang."""
        src = "#![deny(missing_docs)] // Fixed: migrated API\npub fn a() {}\n"
        assert reasons(src, "a.rs") == [(1, "edit-narration")]

    def test_a_shebang_is_skipped(self):
        src = "#!/usr/bin/env python3\nx = 1\n"
        assert reasons(src, "a.py") == []

    def test_a_shebang_does_not_silence_line_two(self):
        """Merged with the shebang, the next line would be skipped with it."""
        src = "#!/usr/bin/env python3\n# Fixed: migrated API\nx = 1\n"
        assert reasons(src, "a.py") == [(2, "edit-narration")]

    def test_a_shebang_is_not_counted_as_a_comment_line(self):
        """Two comments are under `DENSITY_MIN_COMMENT_LINES`. Counted as a
        third, the shebang put this script at 60% comments."""
        src = (
            "#!/bin/sh\n# Build the image, then push it to the registry.\n"
            "# The tag comes from CI.\nset -e\ndocker build .\n"
        )
        assert reasons(src, "a.sh") == []

    def test_a_shebang_never_reaches_a_single_block_judgement(self):
        """Calibration, the bands and corpus sampling read `group_blocks`
        directly, so the drop has to happen before it."""
        src = "#!/usr/bin/env bash\n# Build the image\nset -e\n"
        blocks = cs.group_blocks(cs.extract(src, "a.sh"))
        assert [b.line for b in blocks] == [2]


class TestTrailingAnnotationsAreNotLabels:
    """A trailing comment annotates the value beside it. The tiers whose whole
    signal is terseness read that as a label for the line: `# seconds` alone
    would fire 133 times across this repo."""

    @pytest.mark.parametrize(
        "src,name",
        [
            ("timeout = 5  # seconds\n", "a.py"),
            ("retries = 3  # attempts\n", "a.py"),
            ("size = 1_000_000_000  # 1 GB\n", "a.py"),
            ("start = 0  # first page has no lower bound\n", "a.py"),
            ("epoch = 0  # 1970-01-01 00:00:00 UTC\n", "a.py"),
            ("rows = q[:1]  # Only step 1\n", "a.py"),
            ("skip = True  # If True, skip capacity columns\n", "a.py"),
            ("const t = 5; // seconds\n", "a.ts"),
        ],
    )
    def test_no_finding_on_a_terse_annotation(self, src, name):
        assert reasons(src, name) == []

    @pytest.mark.parametrize(
        "src,reason",
        [
            ("x = 1  # Fixed end_loop indicator for blocks\n", "edit-narration"),
            ("x = 1  # Step 3: normalise\n", "step-marker"),
            ("x = 1  # just to be safe\n", "meta"),
            ("x = 1  # ----------------\n", "banner"),
            ("x = 1  # see services/foo.py:120\n", "code-reference"),
            ("x = 1  # measured 812ms on an M2\n", "benchmark"),
        ],
    )
    def test_inline_ness_does_not_excuse_real_slop(self, src, reason):
        assert reasons(src, "a.py") == [(1, reason)]

    def test_the_same_text_on_its_own_line_still_fires(self):
        """The suppression is about position, not about the text."""
        src = "# seconds\ntimeout = 5\n"
        assert reasons(src, "a.py") == [(1, "bare-identifier")]

    def test_suppression_is_a_flag_on_each_tier(self):
        """A flag on the tier, not a list of names beside the cascade, where a
        typo would silently re-enable a tier for every trailing comment."""
        inline_ok = {t.name: t.inline_ok for t in cs.TIERS}
        assert inline_ok["bare-identifier"] is False
        assert inline_ok["edit-narration"] is True


class TestDensityCountsAMixedLineAsCode:
    def test_three_annotated_assignments_are_not_sixty_percent_comments(self):
        src = (
            "def f():\n    timeout = 5  # seconds\n"
            "    retries = 3  # attempts\n    name = 'x'  # label\n"
            "    return timeout\n"
        )
        raw = cs.extract(src, "a.py")
        counted, neutral = cs.density_sets(raw)
        assert counted == set()
        lines = src.split("\n")
        n_cmt, n_code = cs._density_counts(
            lines, range(1, len(lines) + 1), counted, neutral
        )
        assert (n_cmt, n_code) == (0, 5)
        assert cs.density_finding(src, "a.py", None, counted, neutral) is None

    def test_own_line_comments_still_count(self):
        src = "def f():\n    # one\n    # two\n    # three\n    return 1\n"
        raw = cs.extract(src, "a.py")
        counted, _ = cs.density_sets(raw)
        assert counted == {2, 3, 4}

    def test_inline_travels_on_the_comment(self):
        src = "x = 1  # trailing\n# own line\n"
        by_line = {c.line: c.inline for c in cs.extract(src, "a.py")}
        assert by_line == {1: True, 2: False}


class TestCalibrationCannotDriftFromProduction:
    """`recalibrate.py` measures with the hook's own extraction, exemptions and
    density sets. A private copy of any of them would make the thresholds
    percentiles of a population the hook never sees."""

    def test_one_measure_for_every_language(self):
        """The tests below assert the numbers agree with production. This one
        keeps the SHAPE that makes them agree: a second per-language `measure`
        is a second implementation to drift. Asserted on the module, not on its
        source text -- a grep for `tokenize` fails on a comment mentioning it
        and passes a second lexer written with `re`."""
        assert not hasattr(rc, "measure_generic")

    def test_a_block_veto_leaves_the_word_population(self, tmp_path):
        """TOML exempts a run of commented-out settings as a block, which only
        `exempt` knows; `vetoed` judges one comment's text."""
        src = (
            "[tool.x]\n"
            "# Disabled while the new resolver settles:\n"
            "# a = 1\n# b = 2\n# c = 3\n"
            "z = 1\n"
            "# The resolver reads this before any table, so it stays first\n"
            "y = 2\n"
        )
        path = tmp_path / "p.toml"
        path.write_text(src, encoding="utf-8")
        words, spans, _ = rc.measure(str(path))
        assert words == [11] and spans == [1]

    def test_a_shebang_is_in_neither_population(self, tmp_path):
        src = "#!/bin/sh\n# Build the image\nset -e\ndocker build .\n" + "true\n" * 8
        path = tmp_path / "a.sh"
        path.write_text(src, encoding="utf-8")
        words, spans, dens = rc.measure(str(path))
        assert words == [3] and spans == [1]
        assert dens == [1 / 12]

    def test_vetoed_comments_leave_the_density_numerator(self, tmp_path):
        src = (
            "def g():\n"
            + "    # noqa: E501\n" * 3
            + "".join(f"    a{i} = {i}\n" for i in range(9))
        )
        path = tmp_path / "a.py"
        path.write_text(src, encoding="utf-8")
        words, spans, dens = rc.measure(str(path))
        assert dens == [0.0], "counting vetoed comments reads this as 23%"
        raw = cs.extract(src, str(path))
        assert cs.density_sets(raw)[0] == set()

    def test_a_sphinx_attribute_comment_enters_the_word_population(self, tmp_path):
        """Production strips `#:` and classifies the body. `lstrip("#")` would
        leave a leading `:`, which the downstream veto then drops."""
        src = (
            "class A:\n"
            "    #: The number of retries before giving up on the request\n"
            "    y: int = 2\n"
        )
        path = tmp_path / "a.py"
        path.write_text(src, encoding="utf-8")
        words, _, _ = rc.measure(str(path))
        assert words and words[0] > 5

    def test_trailing_comments_leave_both_populations(self, tmp_path):
        src = "def g():\n" + "".join(f"    a{i} = {i}  # set a{i}\n" for i in range(9))
        path = tmp_path / "a.py"
        path.write_text(src, encoding="utf-8")
        words, spans, dens = rc.measure(str(path))
        assert words == [] and spans == []
        assert dens == [0.0]
