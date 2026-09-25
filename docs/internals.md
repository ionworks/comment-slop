# How it works

Design notes for anyone changing the engine. For install and usage see
[`../README.md`](../README.md).

## Pipeline

    extract  ->  group  ->  scope  ->  exempt  ->  classify  ->  report

`analyze(src, path, added)` is the whole pipeline, with no I/O. `scan()` is the
file-reading, size-capping, `git diff` half in front of it, and `eval/score.py`
calls `analyze` with a labelled case's lines instead of a diff. Nothing else
assembles the steps: a second assembly is a second place to leave out part of
the exemption set.

1. **Extract.** `LANGUAGES` holds one `Lang` record per language: its
   extensions (or, for a Dockerfile, its file-name stems), its lexer, the tiers
   it suppresses, its per-comment and per-block vetoes, and its density
   ceiling. Adding a language is one `Lang` plus one lexer. Each lexer returns
   frozen `Comment` dataclasses carrying their physical lines as a tuple; `span`
   and `body` derive from that tuple, so they cannot drift apart from it.
   `extract()` then stamps `lang` and `inline` on every comment and drops the
   shebang, so no lexer can forget either.
2. **Group.** `group_blocks()` merges contiguous own-line comments of the same
   `kind` at the same column into one `Comment`. A wrapped paragraph is one
   thought, and judged alone a continuation line like `"next turn."` reads as
   narrative slop. Block forms (`/* */`, docstrings) carry `mergeable=False` so
   they never absorb a neighbouring line comment. Pure delimiter lines stay
   standalone, so a run of dashes cannot hide inside neighbouring prose.
3. **Scope.** A block is scanned if the diff touches **any** line of its span.
4. **Exempt.** `exempt()` is the one copy of the exemption set: an empty or
   `#:`-prefixed body, `NEVER`, and the language's `veto` and `block_veto`.
5. **Classify.** `fires()` walks `TIERS`, one ordered tuple of
   `Tier(name, test, inline_ok)`, and the first to fire names the finding.
   `unreviewed` is an opt-in last tier, so it can only ever see a block that
   `exempt` let through and no real tier caught. `block_reason()` is `exempt`
   then `fires` for a single block, which is what corpus generation, snapshot
   scoring and the firing-rate bands call; `recalibrate.py` asks `exempt` alone.
6. **Report.** Exit 2 with the findings, exit 0 clean. Any internal failure
   exits 0.

What the walk leaves — in scope, not exempt, fired on by nothing — is handed to
the optional `sweep` callback, which is how `--llm` (`comment_slop_llm.py`)
joins; `main` imports that module only when `--llm` is passed. The sweep has no
scope or exemption checks of its own, so it cannot disagree with the tiers about
what is in the diff or what is exempt.

`extract()` wraps every lexer call in `try/except Exception` returning `[]`:
a defect in one lexer degrades that file to "no findings" rather than breaking
the edit loop.

## The hook wrapper

Exit 2 is the hook protocol's "block, and show this text to the agent", and
*every* interpreter also exits 2 for its own failures: `uv` on an unwritable
cache, a broken `UV_PYTHON` or an unreadable config; `python3` on a missing
script; `argparse` on a bad flag. Registered directly, a broken toolchain would
report its own error text as comment findings and block the edit loop.

`run_hook.sh` re-derives the exit code instead: findings are exit 2 **and**
output that opens with `comment-slop:`, and anything else exits 0 with its text
still on stderr. Neither half is enough alone — interpreters share the code, and
`warn` shares the prefix on a run that found nothing. It retries under `python3` when `uv` itself failed, so a broken
`uv` degrades to a working fallback rather than to silence, and passes
`--no-python-downloads` so an unmet `.python-version` pin fails over instead of
fetching an interpreter mid-edit. The pin is read from `$CLAUDE_PROJECT_DIR`,
because `uv` would otherwise resolve it from a cwd the hook does not control.

Because the prefix is half the signal, nothing may print ahead of a report.
`ast.parse` does, for an invalid escape like `"\d"`, so `py_docstrings`
silences warnings; otherwise one such string would drop every finding in its
file. The wrapper finds the detector from `$0`, not from the project dir,
because a project-relative path fixes one repository's layout; it follows `$0`
through any symlinks first, since `src/` sits beside the real file, not the link.
A detector it still cannot find exits 1, the protocol's non-blocking error, which
the transcript reports instead of passing every edit as clean.

An installed `comment-slop` needs none of this. Its console script's interpreter
is fixed at install time, so no interpreter resolution runs at edit time to
fail with a 2 of its own.

## Comment kinds

| kind | tiers | classified text | density numerator | density denominator | sweep |
|---|---|---|---|---|---|
| `plain` | all | whole body | yes | yes | yes |
| `doc` | all, with a larger length budget | **chosen prose only** | **no** | **no** | yes |

A doc block's **chosen prose** is its prologue — the text before the first
structured marker, a JSDoc `@tag`, a rustdoc `# Examples`-style section, or a
numpydoc header with its underline — plus the body of every **free-text**
section: numpydoc `Notes` and `Warnings`, rustdoc `# Notes`, TSDoc `@remarks`.
Nothing prescribes what goes in those, which is why an essay ends up there;
truncating at the first marker would exempt them along with the parameter list.
Section headers and their underlines are neither prose nor a prose line.
`doc_prose()` is the one copy of that split, and every semantic tier applies to
what it returns.

Rust keeps `# Panics`, `# Safety` and `# Errors` on the prescribed side. They
are prose, but prose a clippy lint demands for the contract it states, so their
length is not the author's choice either.

Length is kind-aware. Plain comments fire above 30 words or at 3 prose lines;
API-doc prose fires above 90 words or at 10 prose lines. Docstrings are part of
an API contract, so a summary plus a bounded explanation of behavior or
implementation is legitimate. The larger ceiling clears retry and failure-state
docstrings of 29–80 words and 3–9 lines, which the plain ceiling reports again and
again; the next larger shared-helper explanation still crosses both doc limits.

`#:` — the Sphinx/Pydantic attribute-doc marker — is stripped like any other
comment marker and what follows it is a plain comment. A kind of its own,
pairing a 60-word / 7-line ceiling with density neutrality, would let a 58-word
operational narrative on a one-line constant pass: neither tier would see it, so
nothing would. Rendering an annotation into
Sphinx does not make the prose in it less ordinary. The marker is still
recognised rather than left to `lstrip("#")`, because the `:` that would remain
in the body meets the veto on bodies starting with `:` and exempts the comment
outright. `##:` is not the convention, so it keeps meeting that veto.

The split still matters: prescribed parameter and example sections do not spend
the length budget, while an unstructured page of narrative remains visible.
Vetoing `@param` through `NEVER` would suppress the whole block because NEVER is
searched against the joined body.

Doc comments leave **both** sides of the density ratio. Counting an API-doc
construct as code makes a heavily documented file read as comment-sparse;
counting it as comment would flag a file for being documented. Accepted
consequence: a file that is mostly docstring narration will not trip `density`.
The doc tiers and the sweep catch those individually.

## Span semantics

The line ceilings count lines carrying **prose**, not physical lines. A block
form's `/**` and `*/` are not prose. Counting them would make the threshold mean
"three lines of text" for a `#` run but "one" for JSDoc, reporting two-word
comments as verbose and putting the TypeScript doc firing rate at 52.6%.

Scoping and context are both span-aware. A comment is scanned if the diff
touches any of its lines — keyed on the opening line, an edit to line 5 of a
docstring would scan nothing. Context resolves from the block's **last** line, or a
five-line block comment gets explained by lines 2–5 of itself.

## density

The only region-level tier: it asks whether a comment was needed at all, which
no per-comment rule can see.

Its ceiling is per language, `Lang.density_max`, because the number is a
percentile of a **scope** and the scope is not the same everywhere.
`DENSITY_MAX` is 30% because that is roughly p95 of a Python *function*; a shell
script has no scope the hook can see, so the ratio spans the whole file, where a
header comment sits in the numerator with none of the module body a Python file
dilutes it with. Reference practice for a whole shell script is p95 = 38.1% for
PyBaMM and 38.5% for vcpkg, and the Python number reused as-is fires on 8 of
this repo's 48 scripts at `71b1ebf4`, one of them 13 lines long. Shell is set to
40%, above both measurements rather than between them, on the standing rule that
a false positive spends trust a miss does not.

Give a language its own ceiling only from a `recalibrate.py --lang` measurement
over corpora whose comments you would defend, and record it in
[calibration.md](calibration.md). The per-comment ceilings did **not** need the
same treatment for shell — reference blocks trip `verbose` 1.9% of the time — so
scope, not language, is what the exception is about. Vetoed comments are
excluded from its numerator, or a schema file following a lint-directive
convention would be flagged for following it. `density_sets()` builds the
counted and neutral line sets, and `analyze` and `recalibrate.py` share it so
they cannot drift: a calibration that counts every comment token itself, vetoed
ones included, scores a function with three `noqa` lines 23% where production
scores 0%.

A line carrying both code and a comment counts as a **code** line. Counting it as
a comment would inflate the numerator and shrink the denominator at once,
reporting "3 of 5 changed lines are comments (60%)" for a file with no
comment-only line. `Comment.inline` is set once in `extract()`, as `lang` is, so
the merge, the tiers and the density ratio read one answer.

### Comment-only changes

A change carrying no code has no denominator: 100% is the only value the ratio
can take, so a documentation pass cannot clear the bar whatever it writes. A
docs-only workflow of 16 edits would be flagged on every one and proceed past
them all, which is the failure that matters — a tier returning the same verdict
for good and bad input teaches agents to ignore the ones that mean something.

There the whole file is measured instead. This is deliberately a weaker guard:
across this repo, file-wide density runs 0–12.4% against a 30% bar, so it stays
quiet unless a file has genuinely become more comment than code. Exempting
comment-only diffs outright is the alternative, and is wrong because a
standalone narration block added with no code change is the exact shape the
tier exists to catch.

Two orderings carry the behaviour and both have a test that fails if they are
swapped. `DENSITY_MIN_COMMENT_LINES` is checked **before** the fallback, so a
one- or two-line doc tweak never reaches the file measurement. The fallback
applies only when `added is not None`, since a whole-file scan is already
file-scoped and would otherwise be re-measured into a differently worded
finding. The two paths word their message differently on purpose — an author
whose edit was comment-only by design should not be told their edit was 100%
comments.

The fallback triggers at zero code lines only; code-thin diffs (1–2 lines) keep
the diff ratio, because widening it to `n_code < 3` would exempt the narration
shapes the comment floor exists for.

## Verifying a lexer change

Golden tests prove the cases we thought of. The tree-sitter differential proves
agreement with a grammar nobody here wrote, over real files, which is how the
cases we did not think of get found — run it before touching any extractor:

    just test-hooks-oracle

**`just test-hooks` does not collect it.** That recipe installs no tree-sitter, so
collected there the differential would `importorskip` its way to green, eleven
skips standing over whatever lexer bug is in committed code. Excluding the file is
deliberate rather than tidy: a skip the recipe can never satisfy reads as coverage
to anyone scanning the output. The
exclusion is in `tests/conftest.py`, keyed on the same `COMMENT_SLOP_ORACLE_STRICT`
that distinguishes the two recipes, so it survives someone passing their own path;
naming the file explicitly still runs it.
`test-hooks-oracle` is its one home; it installs the grammars and sets
`COMMENT_SLOP_ORACLE_STRICT=1`, which converts a missing grammar or corpus into a
failure. CI runs it that way. A suite that only ever skips gates nothing — that is
the whole reason strict mode exists.

**It reads every matching file, not a sample.** A stride to 250 per language
reads 45% of a 555-file `.tsx` corpus, and with the stride on even indices a lone
disagreeing file at index 349 sits in the unread half while the run reports
agreement. Note the asymmetry with the firing-rate
bands, which *do* sample: a rate is a proportion, and sampling estimates a
proportion correctly. A differential asks whether a disagreement **exists**, and
sampling can only be lucky about that.

Corpora default to this checkout, so TSX, TS, YAML, TOML, shell and Dockerfile
need no setup. `COMMENT_SLOP_ORACLE_APP`, `_REPOS` and `_RUST` override them; Rust is the
one corpus that lives elsewhere, and `test_rust_traps` covers its known traps from
inline source so CI is not blind to it.

`.ts` and `.tsx` are separate grammars because they are separate languages here:
TypeScript forbids JSX in `.ts` so that `<Foo>x` can stay a type assertion, and
the `ts` language turns the JSX scanner off to match. The differential checks
`.ts` files against the TypeScript grammar and `.tsx` against TSX, so each
scanner mode is tested on the files it reads.

**Known disagreement: minified bundles.** Across every local checkout the lexer
agrees with tree-sitter on all 16,953 hand-written JS/TS/TSX files and disagrees
on some of the 527 minified ones, where the template-literal `${...}`
simplification in `_js_string_end` shows up. Generated code carries no comments
worth reporting, and `MAX_BYTES` excludes most of it. The differential's corpora
contain none, so this is recorded rather than fixed — but bundles are where
shifts are dense enough to expose `1 << e` opening a JSX element, so do not
dismiss a bundle disagreement without reading it.

A disagreement is **investigated, not assumed to be ours**. The YAML
disagreements `ACCEPTED` lists are tree-sitter failing on a Helm template or
CloudFormation `!Ref` tags, on comments that are plainly comments. A TypeScript
disagreement can as easily be ours: without the keyword check, `return /re/`
reads as division, and the `"` inside the pattern opens a string that swallows
the next comment in the file.

Shell shows why. Quoting, heredocs, `<<<`, `${x#pre}` and `$((16#ff))` are the
cases anyone writes golden tests for; the one that bites is a constant line
offset. Skip an escaped character with `i += 2` while counting lines, and when
that character is the newline of a continued command the counter falls behind and
every later finding points a line above itself. No golden test would be written
for it, and no reader of a report would suspect the lexer rather than the tier.

That class of bug is closed by construction rather than by care. The four
character-scanning lexers track a string offset and nothing else, and `_locator`
turns an offset into `(line, col)` from a table of line starts, so no lexer
keeps a line counter for a skipped character to desynchronise.

The Dockerfile grammar needs the oracle scoped rather than accepted. It tags a
`#` inside a `RUN`'s shell fragment as a comment, which Docker does not, so
`line_initial` narrows it to the question the extractor answers; and it fails
outright on a continued `ARG`, so `skip_broken` drops those 41 of 185 files.
The remaining 144 agree exactly, with zero over-reports either way.

## Known limitations

- Multi-line quoted YAML scalars.
- A shell script with no recognised extension. `language_of` is given a path, not
  the source, so `#!/usr/bin/env bash` in a file called `deploy` reaches no
  extractor; `.sh`, `.bash`, `.zsh` and `.ksh` do. This repo has no extensionless
  scripts, so shebang sniffing is not worth its cost here.
- A `#` inside a shell heredoc body that the body itself expands
  (`<<EOF` … `$(cmd # comment)`). The body is skipped whole, as a Dockerfile's is.
- A Dockerfile heredoc opened on a `\` continuation line rather than on the
  `RUN`/`COPY`/`ADD` itself; its body is then read as ordinary lines. Restricting
  the search to those three instructions is what stops a `<<` inside an argument
  from opening a heredoc and silencing the rest of the file.
- `bare-identifier` fires on single-token math annotations (`alpha`, `lda`, `p`)
  that name BLAS/LAPACK operands in scientific Rust. Terse by necessity; left
  alone deliberately.

## Classes with no tier

Three named slop classes have **no** tier, because no surface metric finds them.
Ten metrics were scored on their ability to separate deleted comments from kept
ones and every AUC fell within 0.065 of 0.5 — slop is a property of a comment's
relationship to its code, not of its text. These live in the report footer
instead, where they prompt the author on every report:

- mechanism restated instead of reason
- explanation longer than the code it explains
- history that belongs in git
