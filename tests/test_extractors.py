"""Hard-case golden tests for every comment extractor.

Assertions carry (line, col, span, kind, body), not position alone. A lexer
that finds the right comment at the wrong column still corrupts `own_line()`
and the `restates-code` target.
"""

import dataclasses
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs


class TestCommentDataclass:
    def test_defaults_are_plain_single_line_mergeable(self):
        c = cs.Comment(1, 0, ("hello",))
        assert (c.line, c.col, c.body) == (1, 0, "hello")
        assert c.kind == cs.PLAIN
        assert c.span == 1
        assert c.mergeable is True

    def test_span_and_body_derive_from_lines(self):
        """Derived, not stored. Three copies of the classification logic drifted
        apart when span and body were fields alongside the text."""
        c = cs.Comment(1, 0, ("", "first", "second", ""))
        assert c.span == 4
        assert c.body == "first second"

    def test_is_frozen(self):
        c = cs.Comment(1, 0, ("x",))
        with pytest.raises(dataclasses.FrozenInstanceError):
            c.body = "y"


class TestRegistry:
    def test_every_supported_extension_resolves_to_a_callable(self):
        assert cs.SUPPORTED == set(cs.EXTRACTORS)
        for ext, fn in cs.EXTRACTORS.items():
            assert callable(fn), ext

    def test_python_and_js_are_registered(self):
        assert cs.EXTRACTORS[".py"] is cs.py_comments
        assert cs.EXTRACTORS[".tsx"] is cs.js_comments

    def test_ts_is_registered_without_jsx(self):
        """`.ts` is a different language from `.tsx`: TypeScript forbids JSX in
        it so `<Foo>x` can stay a type assertion, and a JSX scanner would read
        that as an element and lose the rest of the file."""
        for ext in (".ts", ".mts", ".cts"):
            assert cs.EXTRACTORS[ext] is cs.ts_comments, ext
        for ext in (".tsx", ".jsx", ".js", ".mjs", ".cjs"):
            assert cs.EXTRACTORS[ext] is cs.js_comments, ext
        assert cs.JSX_EXT | cs.TS_ONLY_EXT == cs.JS_EXT
        assert not cs.JSX_EXT & cs.TS_ONLY_EXT

    def test_extract_returns_empty_for_unknown_extension(self):
        assert cs.extract("# banner\n", "a.txt") == []

    def test_extract_swallows_an_extractor_crash(self, monkeypatch):
        def boom(src):
            raise RuntimeError("unforeseen")

        monkeypatch.setitem(cs.EXTRACTORS, ".py", boom)
        assert cs.extract("# x\n", "a.py") == []


class TestGroupBlocks:
    def test_line_comments_merge_into_one_block(self):
        raw = [cs.Comment(1, 0, ("alpha",)), cs.Comment(2, 0, ("beta",))]
        got = cs._group_blocks(raw, ["# alpha", "# beta", "x = 1"])
        assert len(got) == 1
        assert got[0] == cs.Block(1, 0, ("alpha", "beta"), cs.PLAIN)
        assert got[0].span == 2

    def test_unmergeable_comment_never_absorbs_its_neighbour(self):
        raw = [
            cs.Comment(1, 0, ("block", "more"), cs.PLAIN, mergeable=False),
            cs.Comment(3, 0, ("line",)),
        ]
        got = cs._group_blocks(raw, ["/* block", "   more */", "// line", "x"])
        assert [(b.line, b.span) for b in got] == [(1, 2), (3, 1)]

    def test_span_advances_the_merge_cursor(self):
        """A block spanning 1-3 must not merge with a comment on line 4 by
        treating its own start line as the cursor."""
        raw = [
            cs.Comment(1, 0, ("a", "b", "c"), cs.PLAIN, mergeable=False),
            cs.Comment(4, 0, ("after",)),
        ]
        got = cs._group_blocks(raw, ["/* a", "b", "c */", "// after", "x"])
        assert [(b.line, b.body) for b in got] == [(1, "a b c"), (4, "after")]

    def test_different_kinds_do_not_merge(self):
        raw = [
            cs.Comment(1, 0, ("doc",), cs.DOC),
            cs.Comment(2, 0, ("plain",), cs.PLAIN),
        ]
        got = cs._group_blocks(raw, ["/// doc", "// plain", "x"])
        assert [b.kind for b in got] == [cs.DOC, cs.PLAIN]


class TestScanExtractsOnce:
    def test_scan_calls_the_extractor_exactly_once(self, tmp_path, monkeypatch):
        """scan() called find_findings (which extracted) and then extracted
        again for the density numerator."""
        calls = []
        real = cs.py_comments

        def counting(src):
            calls.append(1)
            return real(src)

        monkeypatch.setitem(cs.EXTRACTORS, ".py", counting)
        p = tmp_path / "a.py"
        p.write_text("# one\n# two\nx = 1\n", encoding="utf-8")
        cs.scan(str(p))
        assert len(calls) == 1


class TestPartialTokenizeRecovery:
    """A write-time hook sees incomplete syntax constantly. Going silent
    exactly while the author is mid-edit is the wrong failure."""

    def test_comments_survive_an_unclosed_paren(self):
        got = cs.py_comments("# ===== TOTALS =====\nx = (\n")
        assert [c.body for c in got] == ["===== TOTALS ====="]

    def test_comments_survive_bad_indentation(self):
        got = cs.py_comments("# banner\n  x = 1\n")
        assert [c.body for c in got] == ["banner"]

    def test_an_unterminated_string_still_yields_nothing(self):
        """The `#` is inside the string, so there is no comment to keep."""
        assert cs.py_comments('s = "open\n# c\n') == []


class TestBareIdentifierPrecision:
    """`.` sat inside the character class, so a one-word sentence matched.
    Measured on a scientific-Rust corpus: 172 -> 155 hits, the 17 dropped
    all being prose fragments like "Workspaces." """

    def test_identifiers_still_match(self):
        for body in [
            "EISStepForm",
            "_prepare_plot_uploads",
            "resource_plan.py",
            "foo.bar.baz",
            "alpha",
        ]:
            f = cs.find_findings(f"# {body}\nreturn render(props)\n", "a.py", None)
            assert [i.reason for i in f] == ["bare-identifier"], body

    def test_one_word_sentences_no_longer_match(self):
        for body in ["Workspaces.", "Serialization.", "constant."]:
            f = cs.find_findings(f"# {body}\nreturn render(props)\n", "a.py", None)
            assert f == [], body


class TestJsBlockComments:
    def test_plain_block_is_extracted_as_plain(self):
        assert cs.js_comments("/* one line */\nconst x = 1;\n") == [
            cs.Comment(1, 0, ("one line",), cs.PLAIN, mergeable=False)
        ]

    def test_jsdoc_block_is_extracted_as_doc(self):
        assert cs.js_comments("/** a doc */\nconst x = 1;\n") == [
            cs.Comment(1, 0, ("a doc",), cs.DOC, mergeable=False)
        ]

    def test_triple_star_is_plain_by_typedoc_convention(self):
        assert cs.js_comments("/*** x */\nconst y = 1;\n")[0].kind == cs.PLAIN

    def test_multiline_block_keeps_one_entry_per_physical_line(self):
        src = "/**\n * first line\n * second line\n */\nconst x = 1;\n"
        got = cs.js_comments(src)
        assert got == [
            cs.Comment(
                1, 0, ("", "first line", "second line", ""), cs.DOC, mergeable=False
            )
        ]
        assert got[0].span == 4
        assert got[0].body == "first line second line"

    def test_block_inside_a_string_is_not_a_comment(self):
        src = 'const s = "/* not */";\nconst x = 1; // real\n'
        assert cs.js_comments(src) == [cs.Comment(2, 13, ("real",))]

    def test_block_inside_a_template_is_not_a_comment(self):
        src = "const t = `a /* not */ b`;\nconst x = 1; // real\n"
        assert cs.js_comments(src) == [cs.Comment(2, 13, ("real",))]

    def test_a_comment_in_a_template_substitution_is_real(self):
        """`${...}` is code. Missing this hid a comment from the whole hook.

        Found by the tree-sitter differential on
        measurement-comparison-view.tsx, where the only two comments the lexer
        disagreed about sat inside a multi-line `${xs.map(...)}`.
        """
        src = (
            "const id = `${xs\n"
            "  .map((x) => {\n"
            "    // why this is cheap\n"
            "    return x;\n"
            "  })\n"
            "  .join('')}`;" + "\n"
        )
        assert cs.js_comments(src) == [cs.Comment(3, 4, ("why this is cheap",))]

    def test_template_text_around_a_substitution_is_still_literal(self):
        src = "const t = `a /* no */ ${x} b // no`;\nconst y = 1; // real\n"
        assert cs.js_comments(src) == [cs.Comment(2, 13, ("real",))]

    def test_nested_template_substitutions_return_to_text(self):
        src = "const t = `${`${a}` + b} // no`;\n// real\n"
        assert cs.js_comments(src) == [cs.Comment(2, 0, ("real",))]

    def test_line_numbers_survive_a_block_comment(self):
        src = "/* a\nb\nc */\nconst x = 1; // here\n"
        assert cs.js_comments(src)[-1] == cs.Comment(4, 13, ("here",))

    def test_column_is_exact_after_a_block_that_ends_mid_line(self):
        """col was reset to 0, so a later comment on the same line reported a
        column before the block it follows. col decides own_line() and picks
        the restates-code target, so this is not cosmetic."""
        src = "const x = /* b */ 1; // real\n"
        got = cs.js_comments(src)
        assert [(c.line, c.col) for c in got] == [(1, 10), (1, 21)]

    def test_unterminated_block_does_not_truncate_the_body(self):
        got = cs.js_comments("/* never closed\nmore text\n")
        assert len(got) == 1
        assert "never closed" in got[0].body and "more text" in got[0].body

    def test_empty_block_yields_an_empty_body(self):
        assert cs.js_comments("/**/\nconst x = 1;\n")[0].body == ""

    def test_pr_1430_jsdoc_block_is_extracted(self):
        """The block that started this: reported clean because js_comments
        found the closing */ and advanced without ever appending."""
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
        got = cs.js_comments(src)
        assert len(got) == 1
        assert got[0].kind == cs.DOC
        assert (got[0].line, got[0].col, got[0].span) == (2, 2, 7)


class TestPyDocstrings:
    def test_module_docstring_is_a_doc_comment(self):
        assert cs.py_docstrings('"""Module doc."""\nx = 1\n') == [
            cs.Comment(1, 0, ("Module doc.",), cs.DOC, mergeable=False)
        ]

    def test_docstring_keeps_one_entry_per_physical_line(self):
        src = 'def f():\n    """One.\n\n    Two.\n    """\n    return 1\n'
        got = cs.py_docstrings(src)
        assert len(got) == 1
        assert (got[0].line, got[0].span) == (2, 4)
        assert got[0].lines == ("One.", "", "Two.", "")
        assert got[0].body == "One. Two."

    def test_class_and_method_docstrings_are_both_found(self):
        src = 'class C:\n    """Klass."""\n    def m(self):\n        """Meth."""\n'
        assert [c.body for c in cs.py_docstrings(src)] == ["Klass.", "Meth."]

    def test_a_bare_string_expression_is_not_a_docstring(self):
        assert cs.py_docstrings('def f():\n    x = 1\n    "not a docstring"\n') == []

    def test_syntax_error_yields_no_docstrings(self):
        assert cs.py_docstrings("def f(:\n") == []

    def test_line_comments_survive_when_ast_fails(self):
        """tokenize and ast are independent failure paths."""
        src = "# ===== TOTALS =====\nx = (\n"
        assert cs.py_docstrings(src) == []
        assert [c.body for c in cs.py_comments(src)] == ["===== TOTALS ====="]

    def test_py_comments_merges_both_sources_in_line_order(self):
        got = cs.py_comments('"""Doc."""\n# a comment\nx = 1\n')
        assert [(c.line, c.kind) for c in got] == [(1, cs.DOC), (2, cs.PLAIN)]


class TestPyAttributeDocs:
    """`#:` is a marker to strip, not a kind of its own.

    Inverted: the Sphinx/Pydantic attribute-doc marker held a `field-doc` kind
    carrying both a 60-word ceiling and density neutrality, and that pair is
    what let a 58-word narrative on a one-line constant through. The marker is
    still recognised, because the `:` it would otherwise leave in the body meets
    the veto that exempts a comment outright; what follows it is judged as an
    ordinary comment.
    """

    def test_own_line_attribute_doc_is_plain_with_the_marker_stripped(self):
        assert cs.py_comments("#: Default timeout in seconds.\nTIMEOUT = 30\n") == [
            cs.Comment(1, 0, ("Default timeout in seconds.",), cs.PLAIN)
        ]

    def test_inline_attribute_doc_is_plain_with_the_marker_stripped(self):
        assert cs.py_comments("TIMEOUT = 30  #: seconds\n") == [
            cs.Comment(1, 14, ("seconds",), cs.PLAIN)
        ]

    def test_consecutive_attribute_docs_merge_into_one_block(self):
        src = "#: The retry budget, shared by\n#: every worker in the pool.\nN = 3\n"
        blocks = cs._group_blocks(cs.py_comments(src), src.split("\n"))
        assert len(blocks) == 1
        assert (blocks[0].kind, blocks[0].span) == (cs.PLAIN, 2)

    def test_attribute_doc_merges_with_a_plain_neighbour(self):
        """Inverted: the kinds no longer differ, so a `#:` line and the `#`
        line under it are one wrapped thought, not two blocks."""
        src = "#: doc\n# plain\nX = 1\n"
        blocks = cs._group_blocks(cs.py_comments(src), src.split("\n"))
        assert [(b.kind, b.span) for b in blocks] == [(cs.PLAIN, 2)]

    def test_double_hash_colon_is_not_the_marker(self):
        """The convention is exactly `#:`; `##:` keeps the ':' in its body, so
        the downstream veto keeps handling it."""
        assert (
            cs.py_comments("##: not the convention\nx = 1\n")[0].body
            == ": not the convention"
        )

    def test_empty_attribute_doc_has_an_empty_body(self):
        assert cs.py_comments("#:\nx = 1\n")[0].body == ""


class TestYamlComments:
    def test_own_line_comment(self):
        assert cs.yaml_comments("# top\nkey: value\n") == [
            cs.Comment(1, 0, ("top",), lang=cs.YAML)
        ]

    def test_inline_comment_after_a_value(self):
        assert cs.yaml_comments("key: value  # trailing\n") == [
            cs.Comment(1, 12, ("trailing",), lang=cs.YAML)
        ]

    def test_hash_without_leading_whitespace_is_part_of_the_scalar(self):
        assert cs.yaml_comments("key: a#b\n") == []

    def test_hash_inside_a_double_quoted_scalar(self):
        assert cs.yaml_comments('key: "# not a comment"\n') == []

    def test_hash_inside_a_single_quoted_scalar(self):
        assert cs.yaml_comments("key: '# not a comment'\n") == []

    def test_block_scalar_body_is_not_scanned(self):
        src = "script: |\n  # not a comment\n  echo hi\nnext: 1  # real\n"
        assert cs.yaml_comments(src) == [cs.Comment(4, 9, ("real",), lang=cs.YAML)]

    def test_every_block_scalar_header_form_is_recognised(self):
        """YAML 1.2.2 8.1.1: | or >, then an optional indentation indicator
        (1-9) and an optional chomping indicator (+/-), in either order."""
        for ind in (
            "|",
            ">",
            "|-",
            ">-",
            "|+",
            ">+",
            "|2",
            ">2-",
            "|+2",
            "|-2",
            "|2-",
            ">9+",
        ):
            assert cs.yaml_comments(f"s: {ind}\n  # not\nk: 1\n") == [], ind

    def test_a_comment_on_the_header_line_is_a_comment(self):
        assert cs.yaml_comments("s: |  # real\n  body\nk: 1\n") == [
            cs.Comment(1, 6, ("real",), lang=cs.YAML)
        ]

    def test_a_plain_scalar_ending_in_gt_is_not_a_header(self):
        assert cs.yaml_comments("cmd: echo >\n  # real\n") == [
            cs.Comment(2, 2, ("real",), lang=cs.YAML)
        ]

    def test_sequence_item_header(self):
        assert cs.yaml_comments("- |\n  # not\nk: 1\n") == []

    def test_blank_line_inside_a_block_scalar_does_not_end_it(self):
        """A blank line has no indentation, so a naive indent comparison ends
        the scalar early and the following body reads as comments."""
        assert cs.yaml_comments("s: |\n  first\n\n  # not\nk: 1\n") == []

    def test_dedent_ends_the_block_scalar(self):
        assert cs.yaml_comments("s: |\n  body\n# real\nk: 1\n") == [
            cs.Comment(3, 0, ("real",), lang=cs.YAML)
        ]

    def test_document_separator_is_not_a_comment(self):
        assert cs.yaml_comments("---\nkey: 1\n...\n") == []

    def test_pr_1454_workflow_comment_is_extracted(self):
        """Reported clean because .yml was not in the extension guard at all."""
        src = (
            "steps:\n"
            "  # The suite runs as a dedicated stage service account:\n"
            "  # INTEGRATION_TEST_API_KEY comes from the staging store.\n"
            "  - name: Fetch secrets\n"
        )
        assert [(c.line, c.col) for c in cs.yaml_comments(src)] == [(2, 2), (3, 2)]


PINNED = (
    "jobs:\n"
    "  build:\n"
    "    steps:\n"
    "      - uses: actions/checkout@08eba0b27e820071cde6df949e0beb9ba4906955"
    "  # v4.2.2 SHA-pin\n"
    "      - uses: astral-sh/setup-uv@e92bafb6253dcd438e0100976411b9d9a6d75a4b"
    "  # v7.0.1\n"
    "      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d870"
    "  # pin to v5.6.0\n"
    "      - run: uv sync\n"
)


class TestYamlShaPinGuard:
    """A SHA-pinned action carries its tag only in the trailing comment, so the
    annotation is the convention. Both firing paths reached it: the bare
    `# v7.0.1` form through `bare-identifier`, the whole pinned block through
    density, since a trailing comment claims its own `uses:` line."""

    def fires(self, src, path="ci.yml"):
        raw = cs.extract(src, path)
        return [cs.block_reason(b) for b in cs._group_blocks(raw, src.split("\n"))]

    def density(self, src, path="ci.yml"):
        counted, neutral = cs.density_sets(cs.extract(src, path))
        return cs.density_finding(src, path, None, counted, neutral)

    def test_pin_annotations_fire_no_tier(self):
        assert self.fires(PINNED) == [None, None, None]

    def test_pin_annotations_leave_the_density_ratio(self):
        assert self.density(PINNED) is None

    def test_a_bare_version_still_fires_outside_yaml(self):
        """The veto is scoped by `lang`, so no other language moves."""
        assert self.fires("# v7.0.1\nx = 1\n", "a.py") == ["bare-identifier"]

    def test_prose_around_a_version_still_fires(self):
        """The guard must not become a blanket exemption for anything numeric."""
        assert self.fires("# Now bump to v7.0.1\nk: 1\n") == ["narrative"]

    @pytest.mark.parametrize(
        "body",
        [
            "v6",
            "v6.2.0",
            "6.2.0",
            "v1.2.3-rc1",
            "v7.0.1 SHA-pin",
            "v7.0.1 (SHA-pinned)",
            "pin to v5.6.0",
            "pinned v5.6.0",
        ],
    )
    def test_vetoed_forms(self, body):
        assert cs.lang_vetoed(cs.YAML, body)

    @pytest.mark.parametrize(
        "body",
        [
            "v4.2.2 released in 2024",
            "v2 API rewrite",
            "3",
            "checkout",
            "v1 and v2 both work",
            "version",
        ],
    )
    def test_forms_the_veto_must_not_reach(self, body):
        assert not cs.lang_vetoed(cs.YAML, body)


def toml_at(src):
    """(line, col, body) triples, so every case need not repeat `lang`."""
    return [(c.line, c.col, c.body) for c in cs.toml_comments(src)]


class TestTomlComments:
    def test_own_line_comment_carries_the_language(self):
        """`lang` is what scopes the two config-file tier guards, so it has to
        survive extraction, not just be set somewhere downstream."""
        assert cs.toml_comments("# top\nkey = 1\n") == [
            cs.Comment(1, 0, ("top",), lang=cs.TOML)
        ]

    def test_inline_comment_after_a_value(self):
        assert toml_at('key = "v"  # trailing\n') == [(1, 11, "trailing")]

    def test_hash_needs_no_leading_whitespace(self):
        """The one rule the YAML scanner gets wrong for TOML: `a#b` is a plain
        scalar in YAML, but bare keys cannot hold `#` and values self-delimit,
        so this is a comment. Confirmed against tree-sitter-toml."""
        assert toml_at("x = 1# tight\n") == [(1, 5, "tight")]

    def test_hash_inside_a_basic_string(self):
        assert toml_at('key = "# not a comment"\n') == []

    def test_hash_inside_a_literal_string(self):
        assert toml_at("key = '# not a comment'\n") == []

    def test_hash_inside_a_multiline_basic_string(self):
        src = 'd = """\n# not a comment\n"""\nk = 1  # real\n'
        assert toml_at(src) == [(4, 7, "real")]

    def test_hash_inside_a_multiline_literal_string(self):
        src = "d = '''\n# not a comment\n'''\nk = 1  # real\n"
        assert toml_at(src) == [(4, 7, "real")]

    def test_position_after_a_multiline_string_is_tracked(self):
        """A construct spanning lines must leave line and col correct, or the
        finding points somewhere the author cannot look."""
        assert toml_at('d = """\na\nb\n"""  # real\n') == [(4, 5, "real")]

    def test_escaped_quote_does_not_end_a_basic_string(self):
        assert toml_at('key = "a \\" # not"  # real\n') == [(1, 20, "real")]

    def test_a_literal_string_has_no_escapes(self):
        """`'a\\'` is a complete literal string whose content is `a\\`; treating
        the backslash as an escape would swallow the real comment after it."""
        assert toml_at("key = 'a\\'  # real\n") == [(1, 12, "real")]

    def test_hash_inside_a_quoted_key(self):
        assert toml_at('"key # not" = 1  # real\n') == [(1, 17, "real")]

    def test_quote_against_the_closing_delimiter(self):
        """TOML permits one or two quotes abutting the delimiter, so the string
        closes at the LAST of the run and the `#` after it is a comment."""
        assert toml_at('d = """he said "hi""""  # real\n') == [(1, 24, "real")]

    def test_unterminated_single_line_string_ends_at_the_newline(self):
        """A single-line string cannot hold a newline. Running past it would
        consume the rest of the file and report nothing in it."""
        assert toml_at('key = "never closed\n# real\n') == [(2, 0, "real")]

    def test_table_headers_and_dates_are_not_comments(self):
        src = "[tool.ruff]\nd = 1979-05-27T07:32:00Z\nk = 1\n"
        assert toml_at(src) == []

    def test_empty_comment_has_an_empty_body(self):
        assert cs.toml_comments("#\nk = 1\n")[0].body == ""

    def test_array_spanning_lines_still_yields_its_comments(self):
        src = 'a = [\n  "F401",  # one\n  "F841",  # two\n]\n'
        assert toml_at(src) == [(2, 11, "one"), (3, 11, "two")]


class TestTomlTierGuards:
    """Two tiers misfire on config-file idioms. Measured over 679 real .toml
    files: these accounted for 24 of 51 findings, none of them correct."""

    def fires(self, src, path="a.toml"):
        raw = cs.extract(src, path)
        lines = src.split("\n")
        return [cs.block_reason(b) for b in cs._group_blocks(raw, lines)]

    def test_commented_out_config_is_not_prose(self):
        """supabase/config.toml disables options by commenting them under one
        line of explanation; the run then reads as a long comment."""
        src = (
            "# Use a production-ready SMTP server\n"
            "# [auth.email.smtp]\n"
            '# host = "smtp.sendgrid.net"\n'
            "# port = 587\n"
            "enabled = true\n"
        )
        assert self.fires(src) == [None]

    def test_a_genuine_prose_block_still_fires(self):
        """The config guard must not become a blanket exemption for TOML."""
        src = (
            "# This comment is deliberately long enough to cross the plain\n"
            "# verbose ceiling of thirty raw words, and it carries no config\n"
            "# syntax at all, so the exemption must not rescue it here.\n"
            "k = 1\n"
        )
        assert self.fires(src) == ["verbose"]

    def test_bare_identifier_is_suppressed_for_toml(self):
        """A lint-ignore label names the linter those opaque codes belong to,
        which is the only information present. Wrong 15 times of 15."""
        assert self.fires('# pyflakes\na = ["F401"]\n') == [None]

    def test_bare_identifier_still_fires_elsewhere(self):
        """The suppression is scoped by `lang`, so no other language moves."""
        assert self.fires("# pyflakes\nx = 1\n", "a.py") == ["bare-identifier"]

    def test_never_still_vetoes_before_the_config_guard(self):
        """Two blocks now, not one: an exempt line no longer merges, so the
        `TODO` is vetoed on its own text and the disabled key is vetoed on its
        own by the config guard. Neither fires either way -- what changed is that
        the `TODO` is no longer what silences the line below it."""
        assert self.fires("# TODO: enable this\n# key = 1\nk = 2\n") == [None, None]


def docker_at(src):
    """(line, col, body) triples, so every case need not repeat `lang`."""
    return [(c.line, c.col, c.body) for c in cs.dockerfile_comments(src)]


class TestDockerfileNaming:
    """A Dockerfile carries its language in its name, so extension lookup alone
    never reached the extractor."""

    @pytest.mark.parametrize(
        "name",
        [
            "Dockerfile",
            "dockerfile",
            "Dockerfile.local",
            "Dockerfile.anyscale",
            "web.dockerfile",
            "Containerfile",
            "Containerfile.dev",
            "build/Dockerfile",
            "DOCKERFILE",
        ],
    )
    def test_recognised_names(self, name):
        assert cs.lang_key(name) in cs.SUPPORTED
        assert cs.extract("# banner\nFROM alpine\n", name)

    @pytest.mark.parametrize(
        "name",
        [
            "docker-compose.yml",
            "Dockerfile.py",
            "dockerignore",
            "notes.txt",
            "dockerfilehelper.go",
        ],
    )
    def test_names_the_dispatch_must_not_claim(self, name):
        assert cs.lang_key(name) != ".dockerfile"

    def test_a_supported_extension_wins_over_the_name(self):
        """`Dockerfile.py` is a Python file someone named oddly; reading it as
        a Dockerfile would drop every docstring and `#:` annotation in it."""
        assert cs.lang_key("Dockerfile.py") == ".py"

    def test_scan_reaches_a_named_dockerfile(self, tmp_path):
        path = tmp_path / "Dockerfile"
        path.write_text("# Helpers\nFROM alpine\n", encoding="utf-8")
        assert [f.reason for f in cs.scan(str(path))] == ["section-label"]


class TestDockerfileComments:
    def test_own_line_comment_carries_the_language(self):
        assert cs.dockerfile_comments("# top\nFROM alpine\n") == [
            cs.Comment(1, 0, ("top",), lang=cs.DOCKER)
        ]

    def test_indented_comment(self):
        assert docker_at("    # indented\nFROM alpine\n") == [(1, 4, "indented")]

    def test_a_hash_after_an_instruction_is_an_argument(self):
        """The one rule that separates this from the YAML scanner: a Dockerfile
        has no trailing comment at all. Everything after the instruction is its
        argument, so reading `#` YAML-style reports shell text as prose the
        author never wrote. Confirmed against tree-sitter-dockerfile."""
        assert docker_at("RUN curl https://x.example/#frag\n") == []
        assert docker_at('LABEL note="a # b"\n') == []
        assert docker_at("RUN #!/bin/bash && build.sh\n") == []

    def test_a_repeated_marker_is_not_part_of_the_body(self):
        """`## Section` is the common Dockerfile banner form. Keeping the extra
        `#` in the body hid it from `section-label`."""
        assert docker_at("## Common build prereqs\nRUN apk add gcc\n") == [
            (1, 0, "Common build prereqs")
        ]

    def test_comment_inside_a_line_continuation(self):
        """The builder strips these the same way it strips any other comment
        line, so the continuation needs no special case."""
        src = "RUN apk add gcc \\\n# and the linker\n    binutils\n"
        assert docker_at(src) == [(2, 0, "and the linker")]

    def test_heredoc_body_is_not_dockerfile_comment_syntax(self):
        """A heredoc body is literal script handed to the shell, the same
        reason YAML skips a block scalar. tree-sitter-dockerfile reads these
        as `heredoc_line`, not `comment`."""
        src = (
            "RUN <<EOF\n# not a dockerfile comment\necho hi\nEOF\n# real\nFROM alpine\n"
        )
        assert docker_at(src) == [(5, 0, "real")]

    def test_quoted_and_dash_heredoc_delimiters(self):
        src = "RUN <<-'EOF'\n\t# not a comment\n\tEOF\n# real\n"
        assert docker_at(src) == [(4, 0, "real")]

    def test_unterminated_heredoc_does_not_swallow_the_file(self):
        """An unclosed delimiter must not silence every later comment, the
        same promise the single-line string forms make in TOML."""
        assert docker_at("RUN <<EOF\necho hi\n# swallowed?\n") == []

    def test_empty_comment_has_an_empty_body(self):
        assert cs.dockerfile_comments("#\nFROM alpine\n")[0].body == ""

    def test_a_run_of_comments_groups_into_one_block(self):
        src = "# one\n# two\nFROM alpine\n"
        blocks = cs._group_blocks(cs.dockerfile_comments(src), src.split("\n"))
        assert [(b.line, b.body) for b in blocks] == [(1, "one two")]


class TestDockerfileDirectiveGuard:
    """Parser directives are configuration written in comment syntax -- the
    builder reads them. `NEVER` already exempts the lint-directive forms of
    every other language; this is the Dockerfile spelling."""

    def fires(self, src, path="Dockerfile"):
        raw = cs.extract(src, path)
        return [cs.block_reason(b) for b in cs._group_blocks(raw, src.split("\n"))]

    @pytest.mark.parametrize(
        "body",
        [
            "syntax=docker/dockerfile:1",
            "syntax=docker/dockerfile:1.4",
            "escape=\\",
            "check=skip=JSONArgsRecommended",
        ],
    )
    def test_vetoed_forms(self, body):
        assert cs.lang_vetoed(cs.DOCKER, body)

    @pytest.mark.parametrize(
        "body",
        [
            "syntax highlighting is off here",
            "escape the quotes below",
            "check that the base image is current",
            "syntax",
        ],
    )
    def test_forms_the_veto_must_not_reach(self, body):
        assert not cs.lang_vetoed(cs.DOCKER, body)

    DIRECTIVES = (
        "# syntax=docker/dockerfile:1\n"
        "# escape=\\\n"
        "# check=skip=JSONArgsRecommended\n"
        "FROM alpine\n"
    )

    def test_a_full_directive_preamble_fires_no_tier(self):
        """Measured without the veto: grouped into one block, the three cross
        the plain word ceiling as `verbose`."""
        assert self.fires(self.DIRECTIVES) == [None, None, None]

    def test_directives_leave_the_density_ratio(self):
        """Measured without the veto: 3 of 4 lines, reported at 75%. Directives
        must precede every instruction, so the preamble is the whole file."""
        counted, neutral = cs.density_sets(cs.extract(self.DIRECTIVES, "Dockerfile"))
        assert (
            cs.density_finding(self.DIRECTIVES, "Dockerfile", None, counted, neutral)
            is None
        )

    def test_a_directive_does_not_absorb_the_comment_below_it(self):
        """Docker recognises a directive only before any other content, so the
        prose a build opens with sits directly beneath one. Merged into a
        single block, the veto on the directive silenced that prose too."""
        src = "# syntax=docker/dockerfile:1\n# Now install the toolchain\nFROM alpine\n"
        assert self.fires(src) == [None, "narrative"]

    def test_the_veto_is_scoped_to_dockerfiles(self):
        assert not cs.lang_vetoed(cs.YAML, "syntax=docker/dockerfile:1")

    def test_prose_in_a_dockerfile_still_fires(self):
        assert self.fires("# Now install the compiler\nRUN apk add gcc\n") == [
            "narrative"
        ]


def sh_at(src):
    """(line, col, body) triples, so every case need not repeat `lang`."""
    return [(c.line, c.col, c.body) for c in cs.shell_comments(src)]


class TestShellComments:
    """Every case here was checked against tree-sitter-bash, and the odd ones
    against bash itself; see `TestShellOracle` in the differential."""

    def test_own_line_comment_carries_the_language(self):
        assert cs.shell_comments("# top\nset -e\n") == [
            cs.Comment(1, 0, ("top",), lang=cs.SHELL)
        ]

    def test_trailing_comment_after_a_command(self):
        assert sh_at("set -e  # why\n") == [(1, 8, "why")]

    def test_hash_inside_quotes_is_not_a_comment(self):
        assert sh_at('echo "a # b"\n') == []
        assert sh_at("echo 'a # b'\n") == []
        assert sh_at("echo $'a \\' # b'\n") == []

    @pytest.mark.parametrize(
        "src",
        [
            "echo it#not\n",  # a word may contain a hash
            'echo "${x#pre}"\n',  # parameter expansion
            "echo $((16#ff))\n",  # arithmetic base notation
            "echo $#\n",  # the argument count
            "echo x \\# not\n",  # escaped
            "case $x in *#*) echo p ;; esac\n",
        ],
    )
    def test_a_hash_that_is_not_at_a_word_start(self, src):
        assert sh_at(src) == []

    @pytest.mark.parametrize(
        "src,col",
        [
            ("echo hi;# after a semicolon\n", 8),
            ("(cd /tmp)# after a subshell\n", 9),
            ("echo `date`# after a backtick\n", 11),
            ("false|# after a pipe\n", 6),
            ("true &# after an ampersand\n", 6),
            ("echo out >#hash\n", 10),
        ],
    )
    def test_a_metacharacter_ends_the_word_before_a_hash(self, src, col):
        """Checked against bash, not reasoned about: even `>#` opens a comment,
        which is why `echo out >#hash` is a syntax error rather than a redirect
        to a file called `#hash`."""
        assert [c.col for c in cs.shell_comments(src)] == [col]

    def test_heredoc_body_is_not_comment_syntax(self):
        src = "cat <<EOF\n# not a comment\necho hi\nEOF\n# real\n"
        assert sh_at(src) == [(5, 0, "real")]

    def test_quoted_and_dash_heredoc_delimiters(self):
        assert sh_at("cat <<-'EOF'\n\t# not\n\tEOF\n# real\n") == [(4, 0, "real")]
        assert sh_at('cat <<"EOF"\n# not\nEOF\n# real\n') == [(4, 0, "real")]
        assert sh_at("cat <<\\EOF\n# not\nEOF\n# real\n") == [(4, 0, "real")]

    def test_two_heredocs_on_one_line_need_both_terminators(self):
        src = "cat <<A <<B\n# not\nA\n# still not\nB\n# real\n"
        assert sh_at(src) == [(6, 0, "real")]

    def test_a_comment_on_the_heredoc_operator_line_is_a_comment(self):
        src = "cat <<EOF  # why\n# not\nEOF\n"
        assert sh_at(src) == [(1, 11, "why")]

    def test_herestring_opens_no_body(self):
        """`<<<` takes an ordinary word, so the lines after it are still code
        and their comments are still comments."""
        assert sh_at('cat <<<"$x"\n# real\n') == [(2, 0, "real")]

    def test_unterminated_heredoc_does_not_swallow_the_file(self):
        """The safe direction, and the trade `dockerfile_comments` makes."""
        assert sh_at("cat <<EOF\necho hi\n# swallowed?\n") == []

    def test_line_continuation_keeps_the_line_count(self):
        """The escaped character is usually the newline of a continued command.
        Skipping it without counting it reported every later comment one line
        above itself, on 9 of this repo's 37 scripts."""
        assert sh_at("cmd one \\\n    two \\\n    three\n# real\n") == [(4, 0, "real")]

    def test_a_string_may_span_lines(self):
        src = 'msg="line one\n# not, still inside the string"\n# real\n'
        assert sh_at(src) == [(3, 0, "real")]

    def test_position_after_a_multiline_string_is_tracked(self):
        src = 'msg="one\ntwo"  # real\n'
        assert sh_at(src) == [(2, 6, "real")]

    def test_a_repeated_marker_is_not_part_of_the_body(self):
        """`## Section` is as common in shell as in a Dockerfile, and keeping
        the extra `#` hid it from `section-label`."""
        assert sh_at("## Helpers\nmain() {\n") == [(1, 0, "Helpers")]

    def test_empty_comment_has_an_empty_body(self):
        assert cs.shell_comments("#\nset -e\n")[0].body == ""

    def test_a_run_of_comments_groups_into_one_block(self):
        src = "# one\n# two\nset -e\n"
        blocks = cs._group_blocks(cs.shell_comments(src), src.split("\n"))
        assert [(b.line, b.body) for b in blocks] == [(1, "one two")]

    def test_a_shebang_does_not_absorb_the_comment_below_it(self):
        """`extract` marks it unmergeable, and `find_findings` skips it. Merged,
        the skip silenced the real comment on line 2."""
        src = "#!/usr/bin/env bash\n# Loads the shared helpers, not sourced\n"
        assert [(c.line, c.mergeable) for c in cs.extract(src, "a.sh")] == [
            (1, False),
            (2, True),
        ]

    @pytest.mark.parametrize("name", ["a.sh", "a.bash", "a.zsh", "a.ksh"])
    def test_every_shell_extension_reaches_the_extractor(self, name):
        assert cs.extract("# top\nset -e\n", name) == [
            cs.Comment(1, 0, ("top",), lang=cs.SHELL)
        ]


class TestShellDensityCeiling:
    """`#` is the comment marker in both Python and shell, so one body can be
    scored as either language -- which is the only way to assert that the
    ceiling, and nothing else, is what differs."""

    # 3 comment lines over 6 of code: 33%, between the two ceilings.
    SRC = "# one\n# two\n# three\na=1\nb=2\nc=3\nd=4\ne=5\nf=6\n"

    def reasons(self, tmp_path, name):
        path = tmp_path / name
        path.write_text(self.SRC, encoding="utf-8")
        return [f.reason for f in cs.scan(str(path), whole_file=True)]

    def test_python_fires_at_thirty_three_percent(self, tmp_path):
        assert "density" in self.reasons(tmp_path, "a.py")

    def test_shell_does_not(self, tmp_path):
        """Reference practice for a whole shell script is p95 = 38.5% (vcpkg)
        and 37.1% (PyBaMM), against the 27.8% of a numpy function that
        DENSITY_MAX comes from."""
        assert "density" not in self.reasons(tmp_path, "a.sh")

    def test_shell_still_fires_above_its_own_ceiling(self, tmp_path):
        path = tmp_path / "b.sh"
        path.write_text("# one\n# two\n# three\na=1\nb=2\nc=3\n", encoding="utf-8")
        assert "density" in [f.reason for f in cs.scan(str(path), whole_file=True)]

    def test_the_message_quotes_the_ceiling_that_applied(self, tmp_path):
        path = tmp_path / "c.sh"
        path.write_text("# one\n# two\n# three\na=1\nb=2\nc=3\n", encoding="utf-8")
        found = [
            f for f in cs.scan(str(path), whole_file=True) if f.reason == "density"
        ]
        assert "under 40%" in found[0].text


class TestShellcheckDirectiveVeto:
    """A shellcheck annotation is machine-readable configuration in comment
    syntax, like `noqa` and the Dockerfile parser directives -- and being in
    NEVER keeps it out of the density numerator too, or a script that follows
    the convention is flagged for following it."""

    @pytest.mark.parametrize(
        "body",
        [
            "shellcheck disable=SC2086",
            "shellcheck shell=bash",
            "shellcheck source=./common.sh",
            "shellcheck disable=SC2034,SC2154",
        ],
    )
    def test_vetoed_forms(self, body):
        assert cs.vetoed(body, cs.SHELL)

    def test_prose_about_the_shell_still_fires(self):
        assert not cs.vetoed("Sets up the shell environment for the test run", cs.SHELL)


class TestRustComments:
    def test_line_comment(self):
        assert cs.rust_comments("let x = 1; // add one\n") == [
            cs.Comment(1, 11, ("add one",))
        ]

    def test_triple_slash_is_a_doc_comment(self):
        assert cs.rust_comments("/// Returns the capacity.\nfn c() {}\n") == [
            cs.Comment(1, 0, ("Returns the capacity.",), cs.DOC)
        ]

    def test_inner_doc_comment(self):
        assert cs.rust_comments("//! Module overview.\nfn c() {}\n") == [
            cs.Comment(1, 0, ("Module overview.",), cs.DOC)
        ]

    def test_four_slashes_is_a_plain_comment(self):
        """rustc treats //// as an ordinary comment, not a doc comment."""
        assert cs.rust_comments("//// not a doc\nfn c() {}\n")[0].kind == cs.PLAIN

    def test_empty_block_is_plain(self):
        assert cs.rust_comments("/**/\nfn f() {}\n")[0].kind == cs.PLAIN

    def test_doc_block_forms(self):
        assert cs.rust_comments("/** a doc */\nfn f() {}\n")[0].kind == cs.DOC
        assert cs.rust_comments("/*! inner doc */\nfn f() {}\n")[0].kind == cs.DOC

    def test_block_comments_nest(self):
        src = "/* outer /* inner */ still outer */\nlet x = 1; // after\n"
        got = cs.rust_comments(src)
        assert len(got) == 2
        assert got[0].kind == cs.PLAIN and "still outer" in got[0].body
        assert got[1] == cs.Comment(2, 11, ("after",))

    def test_slashes_inside_a_string_are_not_a_comment(self):
        src = 'let s = "// not a comment";\nlet x = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_raw_string_contents_are_not_comments(self):
        src = 'let r = r#"also /* not */ one"#;\nlet x = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_multi_hash_raw_string(self):
        src = 'let r = r##"has "# inside // not"##;\nlet x = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_byte_and_byte_raw_strings(self):
        src = 'let a = b"// no";\nlet b = br#"/* no */"#;\nlet x = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(3, 11, ("real",))]

    def test_non_raw_byte_string_honours_escapes(self):
        """b"..." is NOT a raw string. Lexing it with find('"') terminates at
        the escaped quote and exposes the // inside the literal."""
        src = 'let a = b"x \\" // not a comment";\nlet y = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_c_string_literal_honours_escapes(self):
        """Rust 1.77 C-string literal: escapes work like b"..."."""
        src = 'let s = c"// not \\" a comment";\nlet x = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_raw_c_string_with_interior_quote(self):
        """cr#"..."# is raw: an interior quote must not terminate the scan.
        Unhandled, the ordinary string scanner stops at that quote, the
        trailing b"# opens a phantom byte string, and every comment until the
        next quote in the file is swallowed."""
        src = 'let s = cr#"has " and // not"#;\nlet x = 1; // real\n'
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_identifier_ending_in_b_is_not_a_byte_string_prefix(self):
        assert cs.rust_comments('let dylib = "x"; // real\n') == [
            cs.Comment(1, 17, ("real",))
        ]

    def test_lifetime_does_not_swallow_the_file(self):
        """`'a` reads as an unterminated char literal to a naive lexer, which
        then consumes the rest of the file and loses every later comment."""
        src = "fn f<'a>(s: &'a str) -> &'a str { s }\nlet x = 1; // real\n"
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_char_literals_are_skipped(self):
        src = "let c = '/';\nlet n = '\\n';\nlet x = 1; // real\n"
        assert cs.rust_comments(src) == [cs.Comment(3, 11, ("real",))]

    def test_escaped_unicode_char_literal(self):
        src = "let e = '\\u{1F600}';\nlet x = 1; // real\n"
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_labelled_loop_is_not_a_char_literal(self):
        src = "'outer: loop { break 'outer; }\nlet x = 1; // real\n"
        assert cs.rust_comments(src) == [cs.Comment(2, 11, ("real",))]

    def test_line_numbers_survive_a_multiline_block(self):
        src = "/* a\nb\nc */\nlet x = 1; // here\n"
        assert cs.rust_comments(src)[-1] == cs.Comment(4, 11, ("here",))

    def test_columns_are_exact_after_multiline_constructs(self):
        assert cs.rust_comments('let r = r"abc"; // real\n') == [
            cs.Comment(1, 16, ("real",))
        ]
        assert cs.rust_comments('let s = "abc"; // real\n') == [
            cs.Comment(1, 15, ("real",))
        ]
        got = cs.rust_comments("let x = /* b */ 1; // real\n")
        assert [(c.line, c.col) for c in got] == [(1, 8), (1, 19)]

    def test_unterminated_block_does_not_hang(self):
        got = cs.rust_comments("/* never closed\nmore\n")
        assert len(got) == 1 and "never closed" in got[0].body

    def test_consecutive_doc_lines_merge_into_one_block(self):
        src = "/// first\n/// second\n/// third\nfn f() {}\n"
        blocks = cs._group_blocks(cs.rust_comments(src), src.split("\n"))
        assert len(blocks) == 1
        assert blocks[0].span == 3 and blocks[0].kind == cs.DOC


class TestJsRegexLiterals:
    """A `/` after a keyword starts a regex, not a division. `prev_sig` is one
    character, so `return /re/` read as division -- and a `"` inside the
    pattern then opened a string that ran to the next quote in the file,
    swallowing every comment in between. Found by the tree-sitter differential
    on end-condition-editor.tsx, not by any golden case here."""

    def test_regex_after_return_does_not_swallow_later_comments(self):
        src = 'function f(v) {\n  return /^input\\["[^"]+"\\]$/.test(v);\n}\n// real\n'
        assert cs.js_comments(src) == [cs.Comment(4, 0, ("real",))]

    def test_regex_after_every_expression_keyword(self):
        for kw in (
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
        ):
            src = f'const x = {kw} /"/; // real\n'
            assert [c.body for c in cs.js_comments(src)] == ["real"], kw

    def test_division_by_an_identifier_is_still_division(self):
        assert [
            c.body for c in cs.js_comments("const r = total / count; // real\n")
        ] == ["real"]

    def test_an_identifier_ending_in_a_keyword_is_not_a_keyword(self):
        """`myreturn / x` is division. Only a whole word counts."""
        assert [
            c.body for c in cs.js_comments("const r = myreturn / count; // real\n")
        ] == ["real"]

    def test_division_after_a_closing_paren_is_still_division(self):
        assert [c.body for c in cs.js_comments("const r = f(x) / 2; // real\n")] == [
            "real"
        ]

    def test_unterminated_string_stops_at_the_newline(self):
        """JS forbids a raw newline in '' and "". Scanning past one lets a
        single stray quote swallow every comment to the end of the file."""
        assert cs.js_comments('const s = "never closed\n// real\n') == [
            cs.Comment(2, 0, ("real",))
        ]

    def test_a_template_literal_may_still_span_lines(self):
        src = "const t = `line one\nline two`;\n// real\n"
        assert cs.js_comments(src) == [cs.Comment(3, 0, ("real",))]
