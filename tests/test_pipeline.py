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

    def test_by_statement_reads_past_the_lines_inside_a_bracket(self):
        lines = ["# note", "x = f(", "    1,", "    2,", ")", "y = 2"]
        b = cs.Comment(1, 0, ("note",), cs.PLAIN, True)
        assert cs.block_context(lines, b, 2) == ["x = f(", "1,"]
        assert cs.block_context(lines, b, 2, by_statement=True) == [
            "x = f(",
            "1,",
            "2,",
            ")",
            "y = 2",
        ]

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

    def test_a_lowercase_label_after_a_marker_is_its_own_comment(self):
        src = (
            "def f():\n    # TODO: revisit the eviction policy\n"
            "    # fixed: use the cache\n    return 1\n"
        )
        assert reasons(src, "a.py") == [(3, "edit-narration")]

    @pytest.mark.parametrize(
        "veto",
        [
            "# TODO: revisit the eviction policy",
            "# NOTE: keep in sync with the schema",
        ],
    )
    def test_a_lowercase_edit_below_an_unfinished_marker(self, veto):
        """No period ends the marker, so the sentence check alone would read the
        edit as its predicate and exempt both lines."""
        src = f"def f():\n    {veto}\n    # fixed the cache logic\n    return 1\n"
        assert reasons(src, "a.py") == [(3, "edit-narration")]


class TestASentenceWrappedAcrossAnExemptLineStaysWhole:
    """An exempt line keeps out of its neighbours' block, but a sentence that
    runs through it is one comment. Split there, the tail is judged alone from
    mid-sentence, and a long enough tail fires `verbose` on a comment the veto
    exists to protect."""

    def test_a_wrapped_safety_paragraph(self):
        src = (
            "// SAFETY: the pool never touches a cell itself; it only hands\n"
            "// out one address per index, and `create_group` gives each address\n"
            "// to exactly one solver. Distinct solvers therefore mutate distinct\n"
            "// evaluators, and the model they share behind the `Arc` is immutable.\n"
            "unsafe impl Sync for Pool {}\n"
        )
        blocks = cs.group_blocks(cs.extract(src, "a.rs"))
        assert [b.span for b in blocks] == [4]
        assert [cs.block_reason(b) for b in blocks] == [None]

    def test_a_reference_mid_sentence(self):
        src = (
            "def f():\n"
            "    # The burst limit comes from the vendor's rate-limit page at\n"
            "    # https://example.com/limits, which caps a client at ten requests\n"
            "    # a second and answers the eleventh with a 429.\n"
            "    return 1\n"
        )
        assert [b.span for b in cs.group_blocks(cs.extract(src, "a.py"))] == [3]
        assert reasons(src, "a.py") == []

    def test_a_sphinx_role_wrapped_to_the_start_of_a_line(self):
        """`:func:` first reads as a `#:` attribute comment, which is exempt."""
        src = (
            "# NOTE: there is deliberately no exclusion counterpart to\n"
            "# :func:`raise_if_unique_violation`. That helper derives the\n"
            "# message from the exception itself, so the call site is one line.\n"
            "x = 1\n"
        )
        assert [b.span for b in cs.group_blocks(cs.extract(src, "a.py"))] == [3]

    def test_a_directive_is_not_the_rest_of_a_sentence(self):
        """`eslint-disable-next-line` reads as lowercase prose, and joined, its
        veto would silence the explanation above it."""
        src = (
            "// Use primitive IDs in deps to avoid loops from reference changes\n"
            "// eslint-disable-next-line react-hooks/exhaustive-deps\n"
            "useEffect(() => {}, [id]);\n"
        )
        assert [b.span for b in cs.group_blocks(cs.extract(src, "a.ts"))] == [1, 1]

    @pytest.mark.parametrize(
        "above, below",
        [
            ("the buffer is", "moved into the closure, which owns it from here on."),
            ("the handles are shared because they", "moved into the pool together."),
            ("this list is never", "modified after startup, so readers need no lock."),
            ("the entries are lazily", "removed when the cache is swept."),
            (
                "the key is hashed, salted and",
                "added to the set before the lock drops.",
            ),
        ],
    )
    def test_an_edit_verb_after_an_auxiliary_or_subject_continues(self, above, below):
        src = f"// SAFETY: {above}\n// {below}\nunsafe impl Send for Pool {{}}\n"
        blocks = cs.group_blocks(cs.extract(src, "a.rs"))
        assert [b.span for b in blocks] == [2]
        assert [cs.block_reason(b) for b in blocks] == [None]

    def test_a_marker_that_ends_its_sentence_stays_apart(self):
        src = (
            "def f():\n    # NOTE: the cache is warm by now.\n"
            "    # fixed the eviction bug\n    return 1\n"
        )
        assert [b.span for b in cs.group_blocks(cs.extract(src, "a.py"))] == [1, 1]


def fixes(src, name="a.py"):
    """(tier, action, cut) for every per-comment finding on `src`."""
    return [
        (f.reason, f.action, f.cut)
        for f in cs.analyze(src, name)
        if f.reason != "density"
    ]


class TestAFindingSaysWhatToDo:
    """`slop` is fixed by deleting the comment and `trim` by shortening it, so a
    finding has to say which, or following it can delete the fact worth keeping."""

    def test_a_tier_objecting_to_the_whole_comment_deletes_it(self):
        assert fixes("# ===== TOTALS =====\nx = 1\n") == [("banner", "delete", "")]

    def test_a_long_comment_is_shortened(self):
        body = " ".join(["the retry budget covers one slow shard"] * 5)
        assert fixes(f"# {body}\nx = 1\n") == [("verbose", "shorten", "")]

    def test_a_history_clause_is_cut_from_an_explanation_worth_keeping(self):
        src = (
            "# The batch stays at 100 because the provider throttles above that per\n"
            "# key and answers the next call with a 429. It previously sent one.\n"
            "x = 1\n"
        )
        assert fixes(src) == [("historical", "shorten", "It previously sent one.")]

    def test_a_comment_that_is_only_history_is_deleted(self):
        assert fixes("# Previously this returned a dict\nx = 1\n") == [
            ("edit-narration", "delete", "")
        ]

    @pytest.mark.parametrize(
        "body",
        [
            "Step 6: Detect isolated connected components",
            "3. Parse the validated configuration to create the fit object",
        ],
    )
    def test_a_step_number_over_a_label_or_a_narrated_step_deletes_both(self, body):
        """What is left is no longer than `CUT_REST_WORDS`, so it is not a fact."""
        assert fixes(f"# {body}\nx = find(deps)\n") == [("step-marker", "delete", "")]

    def test_a_docstring_keeps_its_summary_however_short(self):
        src = (
            'def upgrade():\n    """Batch upgrade simulations to the parquet format.\n\n'
            "    Re-runs any simulation still stored in the old format.\n"
            '    """\n'
        )
        assert fixes(src) == [
            (
                "historical",
                "shorten",
                "Re-runs any simulation still stored in the old format.",
            )
        ]

    def test_a_docstring_s_sections_are_not_deleted_unread(self):
        """The tiers judge only the prose, so the sections below it stay."""
        doc = '    """Previously this returned a dict.{}"""\n    return [x]\n'
        sections = (
            "\n\n    Parameters\n    ----------\n    x : int\n        The input.\n    "
        )
        assert fixes("def f(x):\n" + doc.format(sections)) == [
            ("edit-narration", "shorten", "Previously this returned a dict.")
        ]
        assert fixes("def f(x):\n" + doc.format("")) == [
            ("edit-narration", "delete", "")
        ]

    def test_what_is_left_is_judged_against_the_code(self):
        src = "# Step 2: load the data\ndata = load()\n"
        assert fixes(src) == [("step-marker", "delete", "")]

    @pytest.mark.parametrize("label", ["Fix:", "Fixed:", "Added:", "Updated:"])
    def test_an_edit_label_is_cut_from_the_reason_it_introduces(self, label):
        src = (
            f"# {label} skip None entries, because the upstream feed emits a null row\n"
            "# per empty shard and the parser rejects every one of them\n"
            "x = 1\n"
        )
        assert fixes(src) == [("edit-narration", "shorten", label)]

    def test_the_rest_s_own_cut_is_named_too(self):
        """Cutting only the first part would leave a second finding to find."""
        src = (
            "# Step 3: refresh the session token whenever the upstream identity\n"
            "# provider rotates its signing keys. Previously we cached it forever.\n"
            "x = 1\n"
        )
        assert fixes(src) == [
            (
                "step-marker",
                "shorten",
                "Step 3: \u2026 Previously we cached it forever.",
            )
        ]

    def test_a_line_pointer_is_cut_from_what_it_points_at(self):
        src = (
            "# Mirrors the cost binding at data_fit.py:1370, which hands each\n"
            "# objective its own bound sub-cost so weights apply per objective\n"
            "x = 1\n"
        )
        assert fixes(src) == [("code-reference", "shorten", "data_fit.py:1370")]

    def test_a_density_finding_asks_for_fewer_comment_lines(self):
        src = "".join(f"# note {i} on the setup here\n" for i in range(4)) + "x = 1\n"
        d = cs.analyze(src, "a.py")[0]
        assert (d.reason, d.action) == ("density", "shorten")

    def test_a_changelog_entry_is_rewritten_or_shortened(self):
        long = " ".join(["word"] * 45) + f" {LINK}"
        src = changelog("Fixed the thing", long)
        assert [(f.reason, f.action) for f in cs.analyze(src, "CHANGELOG.md")] == [
            ("pr-link", "rewrite"),
            ("verbose", "shorten"),
        ]


class TestAHeadingOverAGroup:
    """A heading naming the statements below it is kept; one paraphrasing the
    single thing it sits on is slop."""

    @pytest.mark.parametrize(
        "src, name",
        [
            (
                "interface Filters {\n  // State\n  cycleFilterValue: string;\n"
                "  cycleFilterError: string | null;\n  stepFilterValue: string;\n}\n",
                "a.ts",
            ),
            (
                "fn benches(c: &mut Criterion) {\n    // Robertson\n    {\n"
                '        let mut g = c.benchmark_group("robertson_ci");\n'
                "        bench_implicit!(g, solver);\n    }\n}\n",
                "a.rs",
            ),
            (
                "struct S {\n    // State\n    a: u32,\n    b: u32,\n    c: u32,\n}\n",
                "a.rs",
            ),
            (
                "const x = {\n  // State\n  a: 1,\n  b: 2,\n  c: 3,\n};\n",
                "a.ts",
            ),
            (
                "struct S {\n    // State\n    /// The first.\n    a: u32,\n"
                "    #[serde(skip)]\n    b: u32,\n}\n",
                "a.rs",
            ),
        ],
    )
    def test_a_heading_absent_from_its_group_names_it(self, src, name):
        assert fixes(src, name) == []

    @pytest.mark.parametrize(
        "src, name",
        [
            (
                "class A:\n    # State\n    _cache: dict[str, int] = {\n"
                "        'a': 1,\n        'b': 2,\n        'c': 3,\n    }\n"
                "    _lock = threading.Lock()\n",
                "a.py",
            ),
            (
                "# Limits\nMAX_RETRIES = max(\n    int(os.environ['RETRIES']),\n"
                "    1,\n)\nTIMEOUT = 30\n",
                "a.py",
            ),
            (
                "// Layout\nconst card = {\n  padding: 16,\n  margin: 8,\n};\n"
                "const header = { fontSize: 20 };\n",
                "a.ts",
            ),
        ],
    )
    def test_a_first_statement_over_several_lines_still_opens_a_group(self, src, name):
        """A literal's `{` opens a value, not a block, and the lines inside it do
        not use up the context before the group's second statement."""
        assert fixes(src, name) == []

    def test_a_shared_prefix_can_name_several_fields(self):
        src = (
            "interface Filters {\n  // Step condition state\n"
            "  const [stepConditionValue, setStepConditionValue] = useState('');\n"
            "  const [stepConditionError, setStepConditionError] = useState(null);\n"
            "}\n"
        )
        assert fixes(src, "a.ts") == []

    def test_a_label_repeating_every_name_below_is_still_slop(self):
        src = (
            "const classes = {\n  // button\n  actionButton: c.actionButton,\n"
            "  cancelButton: c.cancelButton,\n  closeButton: c.closeButton,\n};\n"
        )
        assert fixes(src, "a.ts") == [("bare-identifier", "delete", "")]

    def test_the_next_heading_ends_the_group(self):
        src = "def f():\n    # Setup\n    initialise()\n    # Run\n    execute()\n"
        assert fixes(src) == [
            ("section-label", "delete", ""),
            ("bare-identifier", "delete", ""),
        ]

    @pytest.mark.parametrize(
        "src, name",
        [
            ("# Imports\nimport os\nimport sys\n", "a.py"),
            ("// Imports\nimport a from 'a';\nimport b from 'b';\n", "a.ts"),
            ("# Variables\nQ = Variable('Q')\nQt = Variable('Qt')\n", "a.py"),
        ],
    )
    def test_a_plural_label_repeats_its_singular(self, src, name):
        assert [r for r, _, _ in fixes(src, name)] == ["section-label"]

    @pytest.mark.parametrize(
        "src, name",
        [
            (
                "// Setup\nconst client = isProd\n  ? makeProdClient()\n"
                "  : makeDevClient();\n",
                "a.ts",
            ),
            ('# Setup\nif [ -z "$X" ]; then\n  echo hi\nfi\n', "a.sh"),
            ("// Setup\nif (ready) {\n  connect();\n  listen();\n}\n", "a.ts"),
            ("# Setup\n_cache = {\n    'a': 1,\n    'b': 2,\n}\n", "a.py"),
        ],
    )
    def test_one_statement_over_several_lines_is_not_a_group(self, src, name):
        """A formatter's ternary, a compound statement, and a lone literal."""
        assert fixes(src, name) == [("section-label", "delete", "")]

    def test_a_field_note_does_not_end_the_group(self):
        """`#:` documents the field below it, as `///` does in Rust."""
        src = (
            "class S:\n    # State\n    count = 0\n"
            "    #: The last error seen.\n    error = None\n"
        )
        assert fixes(src) == []

    def test_a_field_named_for_a_keyword_is_not_a_definition(self):
        src = (
            "const input = {\n  // Attributes\n  label: 'Email',\n"
            "  type: 'email',\n  required: true,\n};\n"
        )
        assert fixes(src, "a.ts") == []

    def test_a_label_over_one_statement_is_still_slop(self):
        src = "def f(ax):\n    # Plotting\n    ax.legend(loc='best')\n"
        assert fixes(src) == [("bare-identifier", "delete", "")]

    def test_a_label_over_a_definition_names_only_the_definition(self):
        src = "# Helpers\ndef a():\n    return 1\ndef b():\n    return 2\n"
        assert fixes(src) == [("section-label", "delete", "")]

    def test_the_group_ends_where_the_scope_does(self):
        """The test function below is not one of the heading's statements."""
        src = (
            "def test_a():\n    # reset\n    set_logging_level('ERROR')\n"
            "def test_b():\n    assert True\n"
        )
        assert fixes(src) == [("bare-identifier", "delete", "")]

    def test_a_python_dedent_ends_the_group_before_a_module_assignment(self):
        src = "def f():\n    # Setup\n    initialise()\nAFTER = 1\n"
        assert fixes(src) == [("section-label", "delete", "")]

    @pytest.mark.parametrize(
        "src",
        [
            "def f(solution):\n    # Clear cache\n    solution.clear_cache()\n"
            "    assert len(solution.cache) == 0\n",
            "def f(cache):\n    # Clear cache\n    cache.clear()\n"
            "    assert cache.is_clear()\n",
        ],
    )
    def test_narration_over_a_call_is_not_a_group_heading(self, src):
        """The variable recurs below it, which is what variables do. A call and
        an assert declare nothing, so sharing its words makes them no group."""
        assert fixes(src) == [("restates-code", "delete", "")]


class TestAHeadingNamingItsDefinition:
    @pytest.mark.parametrize(
        "src, name",
        [
            ('# Cleanup function\ncleanup() {\n  rm -f "$TMP"\n}\n', "a.sh"),
            (
                "# Helper function to get element value\ndef get_element_value(p):\n"
                "    return p\n",
                "a.py",
            ),
            (
                "// Step Header Component\nexport function StepHeader({ n }: Props) {\n"
                "  return n;\n}\n",
                "a.ts",
            ),
        ],
    )
    def test_the_kind_of_definition_adds_nothing_to_its_name(self, src, name):
        assert fixes(src, name) == [("restates-code", "delete", "")]

    def test_a_kind_noun_still_counts_over_a_call(self):
        """Only a definition says what kind of thing it is."""
        src = "def f(kind):\n    # Get icon for step type\n    getStepIcon(kind)\n"
        assert fixes(src) == []

    @pytest.mark.parametrize(
        "src, name",
        [
            (
                "# Convert enum values to strings\n"
                "def convert_values_to_strings(values):\n    return values\n",
                "a.py",
            ),
            (
                "// Step Header Component\ninterface StepHeaderProps {\n  n: number;\n}\n",
                "a.ts",
            ),
        ],
    )
    def test_a_kind_the_definition_is_not_still_counts(self, src, name):
        """`enum` over a `def` is about its values, not the function."""
        assert fixes(src, name) == []


class TestAFormulaIsContent:
    """A formula, matrix or worked example is as long as its content, so its
    rows do not spend the prose line budget."""

    def test_a_matrix_is_not_verbose(self):
        src = (
            "// dy0/dp = | 0 1 |\n//          | 0 1 |\n"
            "// dy0/dp v = | 0 1 | |v_1| = |v_2|\n//            | 0 1 | |v_2|   |v_2|\n"
            "fn init(v: &V, y: &mut V) {}\n"
        )
        assert fixes(src, "a.rs") == []

    def test_worked_arithmetic_is_not_verbose(self):
        src = (
            "# Cycle 0: 1.0 + 1.0 = 2.0\n# Cycle 1: 2.0 + 0.98 + 1.0 = 3.98\n"
            "# Cycle 2: 3.98 + 0.95 + 1.0 = 5.93\nassert total == 5.93\n"
        )
        assert fixes(src) == []

    def test_formula_words_do_not_spend_the_word_budget(self):
        row = "gradient_1234567890=mass_1234567890+flux_1234567890" * 3
        src = "\n".join("# " + row for _ in range(4)) + "\nresult = compute()\n"
        assert len(cs.RAW_WORD.findall(" ".join([row] * 4))) > cs.VERBOSE_WORDS
        assert fixes(src) == []

    def test_a_banner_title_is_decoration_not_a_formula(self):
        src = (
            "# --- config --------------------------------------------------\n"
            "# Ports are derived per worktree, never hardcoded, so a fixed value\n"
            "# here would be one worktree's ephemeral port.\n"
            "PORT = derive()\n"
        )
        assert fixes(src) == [("verbose", "shorten", "")]

    def test_prose_naming_a_union_type_is_still_prose(self):
        src = (
            '// Resolve the optimizer type from the template: {type: "Pints"|"SciPy"}\n'
            "// in the wrapped format, or the bare name in the flat one, which the\n"
            "// older templates still write.\n"
            "const t = resolve(opt);\n"
        )
        assert fixes(src, "a.ts") == [("verbose", "shorten", "")]


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

    def test_formula_rows_are_in_neither_population(self, tmp_path):
        """`verbose` does not count a formula's rows or words, so the
        thresholds are not percentiles of them either."""
        src = (
            "# Cycle 0: 1.0 + 1.0 = 2.0\n# Cycle 1: 2.0 + 0.98 + 1.0 = 3.98\n"
            "# Totals accumulate per cycle\nassert total == 3.98\n"
        )
        path = tmp_path / "a.py"
        path.write_text(src, encoding="utf-8")
        words, spans, _ = rc.measure(str(path))
        assert words == [4] and spans == [1]

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


LINK = "([#12](https://github.com/o/r/pull/12))"


def changelog(*entries):
    """A changelog whose first entry is on line 6."""
    body = "".join(f"- {e}\n" for e in entries)
    return "# Changelog\n\n## [Unreleased]\n\n### Fixed\n" + body


def verdicts(src, added=None, **kw):
    return [(f.line, f.reason) for f in cs.analyze(src, "CHANGELOG.md", added, **kw)]


class TestChangelogTiers:
    """PyBaMM's guideline: one bullet, ending in its PR link."""

    def test_a_one_line_linked_entry_is_clean(self):
        assert verdicts(changelog(f"`scan` reads a worktree's diff {LINK}")) == []

    @pytest.mark.parametrize(
        "tail",
        [
            "([#3](https://github.com/o/r/issues/3), [#12](https://github.com/o/r/pull/12))",
            "([o/feedstock#70](https://github.com/o/feedstock/pull/70))",
            "([!7](https://gitlab.com/o/r/-/merge_requests/7))",
            f"{LINK}.",
            # Bare, for a changelog mirrored out of a private repo.
            "(#12)",
            "(#12).",
        ],
    )
    def test_accepted_link_forms(self, tail):
        assert verdicts(changelog(f"Fixed the thing {tail}")) == []

    @pytest.mark.parametrize(
        "entry",
        [
            "Fixed the thing",
            "Fixed the thing #12",
            "Fixed the thing (#12) in the parser",
            "Fixed the thing ([#12](https://github.com/o/r/issues/12))",
            f"Fixed {LINK} in the parser",
        ],
    )
    def test_no_pr_link_closing_the_entry(self, entry):
        assert verdicts(changelog(entry)) == [(6, "pr-link")]

    def test_the_number_shown_must_be_the_number_linked(self):
        """A copy-paste slip PyBaMM's own changelog carries twice."""
        entry = "Fixed the thing ([#12](https://github.com/o/r/pull/13))"
        assert verdicts(changelog(entry)) == [(6, "pr-link")]

    def test_a_wrapped_entry(self):
        assert verdicts(changelog(f"Fixed the thing\n  {LINK}")) == [(6, "multi-line")]

    def test_a_nested_list_is_multi_line(self):
        src = changelog(f"Fixed the thing {LINK}\n  - and a detail")
        assert verdicts(src) == [(6, "multi-line")]

    def test_the_word_limit(self):
        at = " ".join(["word"] * cs.CHANGELOG_VERBOSE_WORDS)
        assert verdicts(changelog(f"{at} {LINK}")) == []
        assert verdicts(changelog(f"{at} more {LINK}")) == [(6, "verbose")]

    def test_a_link_counts_its_text_not_its_url(self):
        url = "https://example.com/" + "/".join(["segment"] * 20)
        words = " ".join(["word"] * (cs.CHANGELOG_VERBOSE_WORDS - 3))
        assert verdicts(changelog(f"See [the docs]({url}) {words} {LINK}")) == []

    def test_length_outranks_wrapping(self):
        """Joined onto one line, a long entry would still be too long."""
        long = " ".join(["word"] * (cs.CHANGELOG_VERBOSE_WORDS + 1))
        assert verdicts(changelog(f"{long}\n  {LINK}")) == [(6, "verbose")]

    @pytest.mark.parametrize("rule", ["---", "***", "___", "* * *", "- - -"])
    def test_a_rule_ends_the_entry_and_is_not_one(self, rule):
        src = changelog(f"Fixed the thing {LINK}") + f"{rule}\n"
        assert cs.changelog_entries(src)[-1].lines == (f"Fixed the thing {LINK}",)
        assert verdicts(src) == []

    def test_an_underline_beneath_an_entry_is_part_of_it(self):
        """A setext underline cannot continue a list item, so Markdown renders
        `Release 2` and its `===` as text inside the entry, not as a heading."""
        src = changelog(f"Fixed the thing {LINK}") + "Release 2\n=========\n"
        assert verdicts(src) == [(6, "multi-line")]


class TestAChangelogIsNotACommentFile:
    def test_a_url_or_an_issue_reference_does_not_exempt_an_entry(self):
        """NEVER spares both, and every linked entry carries both."""
        src = changelog("Fixed the thing, see https://example.com and #12")
        assert verdicts(src) == [(6, "pr-link")]

    def test_the_comment_tiers_do_not_apply(self):
        """Edits, history and speed-ups are what a changelog is for."""
        entry = (
            f"Added a cache for the mesh, previously rebuilt per call, 2x faster {LINK}"
        )
        assert verdicts(changelog(entry)) == []

    def test_no_density_finding(self):
        src = changelog(*[f"Fixed thing {i} {LINK}" for i in range(10)])
        assert verdicts(src) == []

    def test_unreviewed_adds_nothing(self):
        assert verdicts(changelog(f"ok {LINK}"), unreviewed=True) == []

    def test_the_model_sweep_is_never_asked(self):
        asked = []
        cs.analyze(
            changelog(f"ok {LINK}"),
            "CHANGELOG.md",
            sweep=lambda residue, lines: asked.append(residue) or [],
        )
        assert asked == []

    def test_only_entries_the_diff_touches_are_judged(self):
        src = changelog("Old entry without a link", f"New entry {LINK}")
        assert verdicts(src, {7}) == []
        assert verdicts(src, {6}) == [(6, "pr-link")]

    def test_editing_a_continuation_line_judges_the_whole_entry(self):
        src = changelog(f"Fixed the thing\n  {LINK}")
        assert verdicts(src, {7}) == [(6, "multi-line")]


class TestChangelogReport:
    def test_the_report_states_the_changelog_convention(self, tmp_path, capsys):
        path = tmp_path / "CHANGELOG.md"
        path.write_text(changelog("Fixed the thing"), encoding="utf-8")
        assert cs.main(["--all", str(path)]) == 2
        err = capsys.readouterr().err
        assert "[pr-link]" in err
        assert cs.CHANGELOG_CONVENTION in err
        assert cs.CONVENTION not in err

    def test_each_convention_is_stated_once(self, tmp_path, capsys):
        (tmp_path / "CHANGELOG.md").write_text(changelog("Fixed"), encoding="utf-8")
        for name in ("a.py", "b.py"):
            (tmp_path / name).write_text(
                "# Fixed: the thing\nx = 1\n", encoding="utf-8"
            )
        assert cs.main(["--all", str(tmp_path)]) == 2
        err = capsys.readouterr().err
        assert err.count(cs.CONVENTION) == 1
        assert err.count(cs.CHANGELOG_CONVENTION) == 1


WHY = "# The writer holds the lock for one poll interval at most.\n"


def strict(src, path="a.py", added=None, base=None):
    return [
        (f.line, f.reason, f.action, f.cut)
        for f in cs.analyze(src, path, added, strict=True, base=base)
    ]


class TestStrictMode:
    """Strict reports every comment a change adds, whatever a tier makes of it.

    New means the file did not already have the text as a comment, so editing
    around someone else's comment never asks for it to be deleted.
    """

    def test_a_comment_no_tier_objects_to_is_still_reported(self):
        src = WHY + "wait_for(lock)\ntimeout = 5  # seconds\n"
        assert cs.analyze(src, "a.py") == []
        assert strict(src) == [
            (1, "strict", "delete", ""),
            (3, "strict", "delete", ""),
        ]

    def test_it_outranks_the_tier_that_would_fire(self):
        assert strict("# Fixed: skip None entries\nx = 1\n") == [
            (1, "strict", "delete", "")
        ]

    def test_a_python_docstring_is_allowed(self):
        src = 'def f():\n    """Return one, the unit of the pack."""\n    return 1\n'
        assert strict(src) == []

    def test_a_docstring_is_still_judged_by_the_tiers(self):
        src = 'def f():\n    """Fixed: skip None entries."""\n    return 1\n'
        assert [reason for _, reason, _, _ in strict(src)] == ["edit-narration"]

    @pytest.mark.parametrize(
        "path, src",
        [
            ("a.ts", "/** Sum two numbers. */\nexport const add = (a, b) => a + b;\n"),
            ("a.rs", "/// Sum two numbers.\npub fn add(a: i32, b: i32) -> i32 {\n"),
            ("a.py", "#: Seconds before a poll gives up.\nTIMEOUT = 5\n"),
        ],
    )
    def test_a_doc_comment_that_is_not_a_docstring_is_reported(self, path, src):
        assert [reason for _, reason, _, _ in strict(src, path)] == ["strict"]

    @pytest.mark.parametrize(
        "path, src",
        [
            ("a.py", "import x  # noqa: E402\n"),
            ("a.py", "y = x.attr  # type: ignore[attr-defined]\n"),
            ("a.ts", "// eslint-disable-next-line no-console\nconsole.log(1);\n"),
            (
                "a.ts",
                "// @ts-expect-error -- the stub has no types\nimport x from 'x';\n",
            ),
            ("a.py", "TABLE = (1, 0,\n         0, 1)  # fmt: skip\n"),
            ("a.py", "import subprocess  # nosec B404 - the command is fixed\n"),
            ("a.sh", "# shellcheck disable=SC2086\necho $x\n"),
            ("a.yml", "- uses: actions/checkout@" + "a" * 40 + "  # v4.1.1\n"),
            ("Dockerfile", "# syntax=docker/dockerfile:1\nFROM alpine\n"),
            ("a.tsx", "// @vitest-environment jsdom\nimport { render } from 'x';\n"),
            ("a.ts", '/// <reference types="vite/client" />\nexport {};\n'),
            ("a.rs", "// SAFETY: `p` came from `Box::into_raw`.\nunsafe { f(p) }\n"),
            (
                "a.rs",
                "// SAFETY: `p` is non-null because it\n"
                "// came from `Box::into_raw` above.\nunsafe { f(p) }\n",
            ),
            ("a.py", "# -*- coding: utf-8 -*-\nx = 1\n"),
            ("a.py", "# vim: set fileencoding=latin-1 :\nx = 1\n"),
            ("a.yml", "# yaml-language-server: $schema=../schema.json\non: push\n"),
            ("a.yml", "key: value  # yamllint disable-line rule:line-length\n"),
            ("Dockerfile", "# hadolint ignore=DL3008\nRUN apt-get install -y curl\n"),
        ],
    )
    def test_a_comment_a_tool_reads_is_allowed(self, path, src):
        assert strict(src, path) == []

    def test_an_encoding_remark_is_not_a_coding_declaration(self):
        src = "# Encoding: the payload is base64, not hex.\nx = 1\n"
        assert strict(src) == [(1, "strict", "delete", "")]

    def test_a_rust_fmt_path_is_not_a_formatter_directive(self):
        src = "// fmt::Display pads the id to eight digits.\nimpl Id {}\n"
        assert strict(src, "a.rs") == [(1, "strict", "delete", "")]

    @pytest.mark.parametrize(
        "comment", ["# TODO: drop once the API ships", "# See https://example.com/spec"]
    )
    def test_what_never_spares_is_reported(self, comment):
        """NEVER guesses that a comment is load-bearing; strict allows none."""
        assert strict(f"{comment}\nx = 1\n") == [(1, "strict", "delete", "")]

    def test_a_reindented_comment_is_not_new(self):
        base = "def f():\n    " + WHY + "    wait_for(lock)\n"
        src = "def f():\n    if ready:\n        " + WHY + "        wait_for(lock)\n"
        assert strict(src, added={2, 3, 4}, base=base) == []

    def test_a_moved_comment_is_not_new(self):
        base = "a()\n" + WHY + "b()\n"
        assert strict(WHY + "a()\nb()\n", added={1}, base=base) == []

    def test_a_second_copy_of_a_comment_is_new(self):
        base = "def f():\n    " + WHY + "    g()\n"
        src = base + "\n\ndef h():\n    " + WHY + "    g()\n"
        assert strict(src, added={4, 5, 6, 7, 8}, base=base) == [
            (7, "strict", "delete", "")
        ]

    @pytest.mark.parametrize("added, line", [({1, 2}, 1), ({3, 4}, 3)])
    def test_either_copy_git_calls_changed_is_the_new_one(self, added, line):
        base = WHY + "g()\n"
        assert strict(base * 2, added=added, base=base) == [
            (line, "strict", "delete", "")
        ]

    @pytest.mark.parametrize(
        "path, src, line",
        [
            ("a.py", "x = 1\n#\ny = 2\n", 2),
            ("a.py", "x = 1  #\n", 1),
            ("a.ts", "const x = 1;\n//\n", 2),
            ("a.ts", "/**/\nconst y = 2;\n", 1),
        ],
    )
    def test_an_empty_comment_is_new(self, path, src, line):
        assert strict(src, path) == [(line, "strict", "delete", "")]

    def test_an_empty_comment_is_named_in_the_report(self):
        [found] = cs.analyze("#\nx = 1\n", "a.py", strict=True)
        assert found.text == "(empty comment)"

    def test_a_blank_line_added_between_paragraphs_adds_nothing(self):
        base = "# One.\n# Two.\nx = 1\n"
        assert strict("# One.\n#\n# Two.\nx = 1\n", added={2}, base=base) == []

    def test_a_moved_empty_comment_is_not_new(self):
        assert strict("#\na()\nb()\n", added={1}, base="a()\n#\nb()\n") == []

    def test_editing_the_code_beside_a_comment_adds_nothing(self):
        base = "timeout = 5  # seconds\n"
        assert strict("timeout = 6  # seconds\n", added={1}, base=base) == []

    def test_a_line_added_to_a_comment_is_cut_and_the_rest_kept(self):
        old = "# The writer holds the lock\n# for one poll interval.\n"
        base = old + "wait_for(lock)\n"
        src = old + "# Measured on the staging replica.\nwait_for(lock)\n"
        assert strict(src, added={3}, base=base) == [
            (1, "strict", "shorten", "Measured on the staging replica.")
        ]

    def test_with_no_earlier_version_every_comment_is_new(self):
        assert strict(WHY + "x = 1\n", added={1, 2}) == [(1, "strict", "delete", "")]

    def test_a_changelog_entry_is_not_a_comment(self):
        assert verdicts(changelog(f"Fixed the thing {LINK}"), strict=True) == []
