"""Regressions for the defects a review of this hook found.

Each class names the defect and asserts the behaviour that was wrong, so a
revert shows up as a failing test rather than as a quiet return of the symptom.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
HOOKS = PKG / "src"
sys.path.insert(0, str(HOOKS))

import comment_slop as cs

# The detector's own walk-up, rather than a parent count: this package has moved
# up the tree once already, and it is the function that has to be right anyway.
REPO_ROOT = Path(cs.repo_root(str(HERE)))


def findings(name, src, **kw):
    """Every finding on `src`, scoped to the whole file rather than a diff.

    `find_findings` never reads the file -- only the extension of `name` is
    consulted, to pick a lexer -- and `added=None` is how production spells
    "whole file" (`scan(whole_file=True)`), so neither a temp file nor an
    all-lines `added` set buys anything here.
    """
    return cs.find_findings(src, name, None, raw=cs.extract(src, name), **kw)


def reasons(name, src, **kw):
    return [(f.line, f.reason) for f in findings(name, src, **kw)]


class TestJsxTextIsNotCode:
    """A path in JSX children opened a block comment. It invented a 56-line
    comment and swallowed the real `{/* ... */}` below it -- the one file in 555
    where this lexer and tree-sitter disagreed."""

    SRC = (
        "export function Docs() {\n"
        "  return (\n"
        '    <Box component="pre">\n'
        "      cp -r ~/ionworks-skills/skills/* ~/.claude/skills/\n"
        "    </Box>\n"
        "  );\n"
        "}\n"
        "export function Other() {\n"
        "  /* Fixed: use the cache */\n"
        "  return <div>hi</div>;\n"
        "}\n"
    )

    def test_a_path_in_jsx_text_is_not_a_comment(self):
        assert [c.line for c in cs.js_comments(self.SRC)] == [9]

    def test_the_real_comment_below_it_survives(self):
        assert reasons("a.tsx", self.SRC) == [(9, "edit-narration")]

    def test_an_apostrophe_in_prose_is_not_a_string(self):
        src = (
            "const A = () => (\n"
            "  <p>it's fine, isn't it</p>\n"
            ");\n"
            "// Refactored the guard\n"
        )
        assert [c.body for c in cs.js_comments(src)] == ["Refactored the guard"]

    def test_a_url_in_jsx_text_is_not_a_line_comment(self):
        src = "const A = () => <p>see https://x.example/y</p>;\n// real\n"
        assert [c.body for c in cs.js_comments(src)] == ["real"]

    def test_the_jsx_comment_form_is_still_found(self):
        src = (
            "const A = () => (\n  <div>\n    {/* Fixed: skip nulls */}\n  </div>\n);\n"
        )
        assert [(c.line, c.body) for c in cs.js_comments(src)] == [
            (3, "Fixed: skip nulls")
        ]

    def test_a_generic_arrow_function_is_not_an_element(self):
        """`<K extends U>` is a type parameter list, which is TypeScript's own
        rule for the ambiguity in a .tsx file. Read as an element it consumed
        the rest of the file as children."""
        src = "const f = <K extends keyof C>(k: K) => k;\n// Updated the mapper\n"
        assert [c.body for c in cs.js_comments(src)] == ["Updated the mapper"]

    def test_a_multi_parameter_generic_is_not_an_element(self):
        src = "const g = <A, B>(a: A) => a;\n// Reworked g\n"
        assert [c.body for c in cs.js_comments(src)] == ["Reworked g"]

    def test_self_closing_elements_do_not_open_a_children_region(self):
        src = 'const A = () => <Icon name="x" />;\n// Tweaked the icon\n'
        assert [c.body for c in cs.js_comments(src)] == ["Tweaked the icon"]

    def test_nested_elements_close_in_order(self):
        src = (
            "const A = () => (\n  <a><b><c>text/*not*/</c></b></a>\n);\n"
            "// Simplified the tree\n"
        )
        assert [c.body for c in cs.js_comments(src)] == ["Simplified the tree"]

    def test_a_ts_file_treats_an_angle_bracket_as_a_type_assertion(self):
        """TypeScript forbids JSX in `.ts` so `<Foo>x` can stay a cast. Reading
        it as an element would swallow the rest of the file."""
        src = "const y = <string>someValue;\n// Renamed the field\n"
        assert [c.body for c in cs.ts_comments(src)] == ["Renamed the field"]
        assert cs.EXTRACTORS[".ts"] is cs.ts_comments
        assert cs.EXTRACTORS[".tsx"] is cs.js_comments

    def test_a_bare_less_than_in_prose_is_text(self):
        """Prose says `5 < 6`. Read as a tag it left a second TEXT frame on the
        stack, so the closing `</p>` popped only one and every later comment was
        inside an element. Caught by the differential's strict trap case."""
        src = "const A = () => (\n  <p>5 < 6 and 7 > 6</p>\n);\n// Widened the range\n"
        assert [c.body for c in cs.js_comments(src)] == ["Widened the range"]

    def test_a_left_shift_is_not_an_element(self):
        """`f = 1 << e`: the first `<` is an operator, which left `prev_sig` as
        `<`, and the second then read as expression position with an identifier
        after it. Found by the differential over minified bundles, where shifts
        are dense -- but `mask = 1 << n` is ordinary source."""
        for src in (
            "const f = 1 << e;\n// real\n",
            "const f=1<<e;\n// real\n",
            "let m = 0; m <<= 3;\n// real\n",
        ):
            assert [c.body for c in cs.js_comments(src)] == ["real"], src

    def test_an_ordinary_comparison_is_not_an_element(self):
        src = "const a = x < y;\n// real\n"
        assert [c.body for c in cs.js_comments(src)] == ["real"]

    def test_a_regex_character_class_may_contain_a_slash(self):
        src = "const re = /[/]/;\n// Loosened the pattern\n"
        assert [c.body for c in cs.js_comments(src)] == ["Loosened the pattern"]


class TestAnExemptLineDoesNotSilenceItsNeighbours:
    """`NEVER` was searched against the JOINED body of a grouped block, so one
    exempt line vetoed every comment merged with it."""

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
        assert reasons("a.py", src) == [(3, "edit-narration")]

    def test_an_exempt_line_below_a_finding(self):
        src = (
            "def f():\n    # Fixed: use the cache\n"
            "    # TODO: revisit later\n    return 1\n"
        )
        assert reasons("a.py", src) == [(2, "edit-narration")]

    def test_a_genuine_wrapped_paragraph_still_merges(self):
        """The merge exists so a continuation is not judged out of context. It
        has to survive the fix."""
        src = (
            "def f():\n"
            "    # The cache is keyed on the request id rather than the URL,\n"
            "    # because two callers can ask for the same URL in one turn.\n"
            "    return 1\n"
        )
        blocks = cs._group_blocks(cs.extract(src, "a.py"), src.split("\n"))
        assert [b.span for b in blocks] == [2]
        assert reasons("a.py", src) == []

    def test_an_exempt_comment_is_still_exempt_on_its_own(self):
        src = "def f():\n    # TODO: revisit the eviction policy\n    return 1\n"
        assert reasons("a.py", src) == []


class TestTheShebangExceptionIsPythonOnly:
    def test_rust_inner_doc_on_line_one_is_judged(self):
        """`//!` is crate documentation, not a shebang. Skipping line 1 meant a
        slop comment in a crate's opening block was missed while the identical
        comment one line down fired."""
        src = "//! Fixed: migrated API\npub fn a() {}\n"
        assert reasons("a.rs", src) == [(1, "edit-narration")]

    def test_the_same_comment_lower_down_still_fires(self):
        src = "pub fn z() {}\n//! Fixed: migrated API\npub fn a() {}\n"
        assert reasons("a.rs", src) == [(2, "edit-narration")]

    def test_a_shebang_is_still_skipped(self):
        src = "#!/usr/bin/env python3\nx = 1\n"
        assert reasons("a.py", src) == []

    def test_a_shebang_does_not_silence_line_two(self):
        """The shebang absorbed the next line into its block, and the line-1
        skip then silenced a real finding there."""
        src = "#!/usr/bin/env python3\n# Fixed: migrated API\nx = 1\n"
        assert reasons("a.py", src) == [(2, "edit-narration")]


class TestTrailingAnnotationsAreNotLabels:
    """A trailing comment annotates the value beside it. The tiers whose whole
    signal is terseness read that as a label for the line: `# seconds` alone
    fired 133 times across this repo."""

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
        assert reasons(name, src) == []

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
        assert reasons("a.py", src) == [(1, reason)]

    def test_the_same_text_on_its_own_line_still_fires(self):
        """The suppression is about position, not about the text."""
        src = "# seconds\ntimeout = 5\n"
        assert reasons("a.py", src) == [(1, "bare-identifier")]

    def test_suppression_is_recorded_as_a_set_not_scattered(self):
        assert "bare-identifier" in cs.INLINE_SUPPRESSED
        assert "edit-narration" not in cs.INLINE_SUPPRESSED


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


class TestBenchmarkSparesNumbersThatCarryAnArgument:
    """A cost that justifies a decision is a premise, not a snapshot: deleting
    the number deletes the reason. Both of the tier's hits on this hook's own
    source were of that shape."""

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
        src = (HOOKS / "comment_slop.py").read_text(encoding="utf-8")
        got = findings("comment_slop.py", src)
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
        """`--include-unreviewed` advertised a deliberate sweep, but diff
        scoping meant a clean tracked file reported nothing at all."""
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


class TestTheHookWrapperOnlyExitsTwoForFindings:
    """Exit 2 means "block and show this to the agent". Every interpreter also
    exits 2 for its own failures, so the code is re-derived from the output."""

    WRAPPER = PKG / "run_hook.sh"

    def run(self, path, env=None, tmp_path=None):
        full = dict(os.environ, CLAUDE_PROJECT_DIR=str(REPO_ROOT))
        full.update(env or {})
        r = subprocess.run(
            ["sh", str(self.WRAPPER)],
            input=json.dumps({"tool_input": {"file_path": str(path)}}),
            capture_output=True,
            text=True,
            env=full,
            timeout=120,
        )
        return r.returncode, r.stderr

    @pytest.fixture
    def clean(self, tmp_path):
        p = tmp_path / "clean.py"
        p.write_text("x = 1\n", encoding="utf-8")
        return p

    @pytest.fixture
    def dirty(self, tmp_path):
        p = tmp_path / "dirty.py"
        p.write_text("# Fixed: use the cache\nx = 1\n", encoding="utf-8")
        return p

    @pytest.fixture
    def fake_root(self, tmp_path):
        """A project dir with the layout the wrapper expects and a version file
        of the test's choosing."""

        def make(version=None):
            root = tmp_path / "proj"
            (root / "packages").mkdir(parents=True)
            (root / "packages" / "comment-slop").symlink_to(PKG)
            if version is not None:
                (root / ".python-version").write_text(version, encoding="utf-8")
            return {"CLAUDE_PROJECT_DIR": str(root)}

        return make

    def test_findings_exit_two(self, dirty):
        code, err = self.run(dirty)
        assert code == 2 and err.startswith("comment-slop: ")

    def test_a_clean_file_exits_zero(self, clean):
        assert self.run(clean)[0] == 0

    def test_a_broken_uv_cache_does_not_report_findings(self, clean, tmp_path):
        bad = tmp_path / "nocache"
        bad.mkdir(mode=0o000)
        try:
            code, _ = self.run(clean, {"UV_CACHE_DIR": str(bad)})
        finally:
            bad.chmod(0o755)
        assert code == 0

    def test_a_broken_uv_still_reports_real_findings(self, dirty, tmp_path):
        """The fallback matters: a broken uv must degrade to python3, not to
        silence."""
        bad = tmp_path / "nocache2"
        bad.mkdir(mode=0o000)
        try:
            code, err = self.run(dirty, {"UV_CACHE_DIR": str(bad)})
        finally:
            bad.chmod(0o755)
        assert code == 2 and "edit-narration" in err

    def test_the_pinned_version_is_read_from_the_project_dir(
        self, dirty, fake_root, tmp_path
    ):
        """uv finds `.python-version` by walking up from its cwd, which the hook
        does not control -- from outside the repo it picks a different
        interpreter. So the wrapper reads the project dir's version and passes
        it. A version uv cannot satisfy is what makes that visible: the run has
        to reach the fallback, and the stub announces itself there. Unpinned,
        uv would succeed and report findings instead."""
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "python3").write_text(
            "#!/bin/sh\nprintf 'fallback ran\\n'\n", encoding="utf-8"
        )
        (stub / "python3").chmod(0o755)
        env = fake_root("9.9\n")
        env["PATH"] = f"{stub}:{os.environ['PATH']}"
        code, err = self.run(dirty, env)
        assert code == 0 and "fallback ran" in err

    def test_the_pin_never_triggers_an_interpreter_download(self, dirty, tmp_path):
        """`uv run --python=X` satisfies an unmet pin by *downloading* X, so a
        post-edit hook would block on a network fetch to run a stdlib-only
        script. --no-python-downloads is what makes the comment above the pin
        true: unsatisfiable has to fail, so the fallback can run. The real
        version is satisfiable on any dev machine, so a stub uv records the argv
        instead — and exits nonzero, standing in for the pin it cannot meet."""
        stub = tmp_path / "bin"
        stub.mkdir()
        argv = tmp_path / "argv.txt"
        (stub / "uv").write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$@" >"{argv}"\nexit 1\n', encoding="utf-8"
        )
        (stub / "uv").chmod(0o755)
        code, err = self.run(dirty, {"PATH": f"{stub}:{os.environ['PATH']}"})
        assert "--no-python-downloads" in argv.read_text().split("\n")
        # And the run still degrades to python3 rather than to silence.
        assert code == 2 and "edit-narration" in err

    def test_a_broken_interpreter_choice_does_not_report_findings(
        self, clean, fake_root
    ):
        env = fake_root()
        # No version file, so UV_PYTHON is again what chooses the interpreter.
        env["UV_PYTHON"] = "python9.9"
        assert self.run(clean, env)[0] == 0

    def test_no_uv_on_path_still_works(self, dirty):
        code, err = self.run(dirty, {"PATH": "/usr/bin:/bin"})
        assert code == 2 and "edit-narration" in err

    def test_a_malformed_payload_exits_zero(self):
        r = subprocess.run(
            ["sh", str(self.WRAPPER)],
            input="not json",
            capture_output=True,
            text=True,
            env=dict(os.environ, CLAUDE_PROJECT_DIR=str(REPO_ROOT)),
            timeout=120,
        )
        assert r.returncode == 0

    def test_settings_json_calls_the_wrapper(self):
        cfg = json.loads(
            (REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
        )
        cmds = [
            h["command"]
            for entry in cfg["hooks"]["PostToolUse"]
            for h in entry["hooks"]
        ]
        assert any("run_hook.sh" in c for c in cmds)
        assert not any("comment_slop.py --hook" in c for c in cmds), (
            "settings.json must not invoke the detector directly -- the "
            "interpreter's own exit 2 would read as findings"
        )


class TestCalibrationCannotDriftFromProduction:
    """`recalibrate.py` had its own extraction and its own density arithmetic.
    They disagreed with production about `#:` bodies and about vetoed and
    trailing comments, so the thresholds were percentiles of a population the
    hook never sees."""

    def test_one_measure_for_every_language(self):
        """The three tests below assert the numbers agree with production. This
        one keeps the SHAPE that makes them agree: a second per-language
        `measure` is how the two drifted in the first place. Asserted on the
        module, not on its source text -- a grep for `tokenize` fails on a
        comment mentioning it and passes a second lexer written with `re`."""
        import recalibrate as rc

        assert not hasattr(rc, "measure_generic")

    def test_vetoed_comments_leave_the_density_numerator(self, tmp_path):
        import recalibrate as rc

        src = (
            "def g():\n"
            + "    # noqa: E501\n" * 3
            + "".join(f"    a{i} = {i}\n" for i in range(9))
        )
        path = tmp_path / "a.py"
        path.write_text(src, encoding="utf-8")
        words, spans, dens = rc.measure(str(path))
        assert dens == [0.0], "recalibrate scored these 23% against 0%"
        raw = cs.extract(src, str(path))
        assert cs.density_sets(raw)[0] == set()

    def test_a_sphinx_attribute_comment_enters_the_word_population(self, tmp_path):
        """Production strips `#:` and classifies the body. `lstrip("#")` left a
        leading `:`, which the downstream veto then dropped."""
        import recalibrate as rc

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
        import recalibrate as rc

        src = "def g():\n" + "".join(f"    a{i} = {i}  # set a{i}\n" for i in range(9))
        path = tmp_path / "a.py"
        path.write_text(src, encoding="utf-8")
        words, spans, dens = rc.measure(str(path))
        assert words == [] and spans == []
        assert dens == [0.0]
