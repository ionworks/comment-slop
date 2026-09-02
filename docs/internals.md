# How it works

Design notes for anyone changing the engine. For install and usage see
[`../README.md`](../README.md).

## Pipeline

    extract  ->  group  ->  classify  ->  scope  ->  report

1. **Extract.** `EXTRACTORS` is a registry keyed by file extension. Each
   extractor returns frozen `Comment` dataclasses carrying their physical lines
   as a tuple; `span` and `body` derive from that tuple, so they cannot drift
   apart from it. Adding a language is one registry entry plus one extractor.
2. **Group.** Contiguous own-line comments of the same `kind` at the same column
   become one `Block`. A wrapped paragraph is one thought, and judging
   continuation lines alone made `"next turn."` read as narrative slop. Block
   forms (`/* */`, docstrings) carry `mergeable=False` so they never absorb a
   neighbouring line comment. Pure delimiter lines stay standalone, so a run of
   dashes cannot hide inside neighbouring prose.
3. **Classify.** `block_reason()` is the single copy of the tier cascade —
   vetoes, then `restates-code`, then `PATTERNS`, then `LATE_TIERS`, then the
   kind-aware word and span checks. Calibration, corpus generation and the hook
   all call it, because three near-copies previously disagreed about what
   fires.
4. **Scope.** A comment is scanned if the diff touches **any** line of its span.
5. **Report.** Exit 2 with the findings, exit 0 clean. Any internal failure
   exits 0.

`extract()` wraps every extractor call in `try/except Exception` returning `[]`:
a defect in one lexer degrades that file to "no findings" rather than breaking
the edit loop.

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
truncating at the first marker exempted them along with the parameter list.
Section headers and their underlines are neither prose nor a prose line.
`doc_prose()` is the one copy of that split, and every semantic tier applies to
what it returns.

Rust keeps `# Panics`, `# Safety` and `# Errors` on the prescribed side. They
are prose, but prose a clippy lint demands for the contract it states, so their
length is not the author's choice either.

Length is kind-aware. Plain comments fire above 30 words or at 3 prose lines;
API-doc prose fires above 90 words or at 10 prose lines. Docstrings are part of
an API contract, so a summary plus a bounded explanation of behavior or
implementation is legitimate. The larger ceiling was set after retry and
failure-state docstrings of 29–80 words and 3–9 lines produced repeated false
positives. The next larger shared-helper explanation crossed both doc limits.

`#:` — the Sphinx/Pydantic attribute-doc marker — is stripped like any other
comment marker and what follows it is a plain comment. It held a `field-doc`
kind pairing a 60-word / 7-line ceiling with density neutrality, and the two
together are what let a 58-word operational narrative on a one-line constant
pass: neither tier could see it, so nothing did. Rendering an annotation into
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
form's `/**` and `*/` are not prose. Counting them made the threshold mean
"three lines of text" for a `#` run but "one" for JSDoc, which reported two-word
comments as verbose and took the TypeScript doc firing rate to 52.6%.

Scoping and context are both span-aware. A comment is scanned if the diff
touches any of its lines — keying on the opening line meant editing line 5 of a
docstring scanned nothing. Context resolves from the block's **last** line, or a
five-line block comment gets explained by lines 2–5 of itself.

## density

The only region-level tier: it asks whether a comment was needed at all, which
no per-comment rule can see.

Its ceiling is per language, in `DENSITY_CEILINGS`, because the number is a
percentile of a **scope** and the scope is not the same everywhere.
`DENSITY_MAX` is 30% because that is roughly p95 of a Python *function*; a shell
script has no scope the hook can see, so the ratio spans the whole file, where a
header comment sits in the numerator with none of the module body a Python file
dilutes it with. Reference practice for a whole shell script is p95 = 38.5%
(vcpkg) and 37.1% (PyBaMM), and the Python number reused as-is fired on 9 of this
repo's 37 scripts, one of them 14 lines long. Shell is set to 40%, above both
measurements rather than between them, on the standing rule that a false positive
spends trust a miss does not.

Add a language to that table only from a `recalibrate.py --lang` measurement over
corpora whose comments you would defend, and record it in
[calibration.md](calibration.md). The per-comment ceilings did **not** need the
same treatment for shell — reference blocks trip `verbose` 1.9% of the time — so
scope, not language, is what the exception is about. Vetoed comments are excluded from its numerator, or
a schema file following a lint-directive convention would be flagged for
following it. `density_sets()` builds the counted and neutral line sets, and is
shared with `eval/score.py` and `recalibrate.py` so the three cannot drift —
`recalibrate.py` used to count every comment token itself, including the vetoed
ones, and scored a function 23% where production scored 0%.

A line carrying both code and a comment counts as a **code** line. Counting it as
a comment inflated the numerator and shrank the denominator at once, reporting
"3 of 5 changed lines are comments (60%)" for a file with no comment-only line.
`Comment.inline` is set once in `extract()`, for the same reason `lang` is: three
call sites judged inline-ness for themselves and disagreed.

### Comment-only changes

A change carrying no code has no denominator: 100% is the only value the ratio
can take, so a documentation pass could not clear the bar whatever it wrote. A
docs-only workflow hit this on all 16 of its edits and proceeded past every
one, which is the failure that matters — a tier returning the same verdict for
good and bad input teaches agents to ignore the ones that mean something.

There the whole file is measured instead. This is deliberately a weaker guard:
across this repo, file-wide density runs 0–12.4% against a 30% bar, so it stays
quiet unless a file has genuinely become more comment than code. Exempting
comment-only diffs outright was the alternative, and was rejected because a
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
shapes the comment floor was added for.

## Verifying a lexer change

Golden tests prove the cases we thought of. The tree-sitter differential proves
agreement with a grammar nobody here wrote, over real files, which is how the
cases we did not think of get found — run it before touching any extractor:

    just test-hooks-oracle

**`just test-hooks` does not collect it.** That recipe installs no tree-sitter, so
the differential `importorskip`ped its way to green: nine tests skipped, suite
green, and a real `.tsx` lexer bug in committed code. Excluding the file is
deliberate rather than tidy — left in the run it reported 11 skips, and a skip the
recipe can never satisfy reads as coverage to anyone scanning the output. The
exclusion is in `tests/conftest.py`, keyed on the same `COMMENT_SLOP_ORACLE_STRICT`
that distinguishes the two recipes, so it survives someone passing their own path;
naming the file explicitly still runs it.
`test-hooks-oracle` is its one home; it installs the grammars and sets
`COMMENT_SLOP_ORACLE_STRICT=1`, which converts a missing grammar or corpus into a
failure. CI runs it that way. A suite that only ever skips gates nothing — that is
the whole reason strict mode exists.

**It reads every matching file, not a sample.** It used to stride to 250 per
language. The one `.tsx` file in 555 that disagreed was at index 349 and the
stride took the even indices, so it reported agreement over 45% of the corpus with
the offending file in the unread half. Note the asymmetry with the firing-rate
bands, which *do* sample: a rate is a proportion, and sampling estimates a
proportion correctly. A differential asks whether a disagreement **exists**, and
sampling can only be lucky about that.

Corpora default to this checkout, so TSX, TS, YAML, TOML, shell and Dockerfile
need no setup. `COMMENT_SLOP_ORACLE_APP`, `_REPOS` and `_RUST` override them; Rust is the
one corpus that lives elsewhere, and `test_rust_traps` covers its known traps from
inline source so CI is not blind to it.

`.ts` and `.tsx` are separate grammars because they are separate languages here:
TypeScript forbids JSX in `.ts` so that `<Foo>x` can stay a type assertion, and
`ts_comments` turns the JSX scanner off to match. `.ts` had no differential at all
until this was found.

**Known disagreement: minified bundles.** Across every local checkout the lexer
agrees with tree-sitter on all 16,953 hand-written JS/TS/TSX files and disagrees
on some of the 527 minified ones, where the template-literal `${...}`
simplification in `_js_string_end` shows up. Generated code carries no comments
worth reporting, and `MAX_BYTES` excludes most of it. The differential's corpora
contain none, so this is recorded rather than fixed — but it is how the left-shift
bug (`1 << e` opening a JSX element) was found, so do not dismiss a bundle
disagreement without reading it.

A disagreement is **investigated, not assumed to be ours**. Every YAML
disagreement so far was tree-sitter failing on a Helm template or CloudFormation
`!Ref` tags, on comments that are plainly comments; those are listed in
`ACCEPTED`. The one TypeScript disagreement was ours: `return /re/` read as
division, and the `"` inside the pattern opened a string that swallowed the next
comment in the file.

Shell is where the differential earned its keep most cheaply. The lexer agreed
with tree-sitter-bash on quoting, heredocs, `<<<`, `${x#pre}` and `$((16#ff))`
first time, and disagreed on 9 of this repo's 37 scripts by a constant line
offset: an escaped character was skipped with `i += 2`, and when that character is
the newline of a continued command the line counter falls behind and every later
finding points a line above itself. No golden test would have been written for it,
and no reader of a report would have suspected the lexer rather than the tier.

The Dockerfile grammar needed the oracle scoped rather than accepted. It tags a
`#` inside a `RUN`'s shell fragment as a comment, which Docker does not, so
`line_initial` narrows it to the question the extractor answers; and it fails
outright on a continued `ARG`, so `skip_broken` drops those 41 of 185 files.
The remaining 144 agree exactly, with zero over-reports either way.

## Known limitations

- Multi-line quoted YAML scalars.
- A shell script with no recognised extension. `lang_key` is given a path, not
  the source, so `#!/usr/bin/env bash` in a file called `deploy` reaches no
  extractor; `.sh`, `.bash`, `.zsh` and `.ksh` do. This repo has no extensionless
  scripts, so shebang sniffing is not paid for yet.
- A `#` inside a shell heredoc body that the body itself expands
  (`<<EOF` … `$(cmd # comment)`). The body is skipped whole, as a Dockerfile's is.
- A Dockerfile heredoc opened on a `\` continuation line rather than on the
  `RUN`/`COPY`/`ADD` itself; its body is then read as ordinary lines. Restricting
  the search to those three instructions is what stops a `<<` inside an argument
  from opening a heredoc and silencing the rest of the file.
- Comments inside JS template-literal interpolations
  (`` `${ /* here */ x }` ``) — measured at zero occurrences in the calibration
  corpora.
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
