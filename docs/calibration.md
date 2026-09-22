# Thresholds and calibration

Where every constant came from, and how to re-derive it.

## Re-deriving

    uv run --with numpy --with scipy --with pandas python recalibrate.py
    uv run python recalibrate.py --lang rust --roots ~/path/to/crate
    uv run python recalibrate.py --lang yaml --roots ~/path/to/repo

Prints the reference percentiles next to the constants currently in use, so the
thresholds can be checked against a newer numpy/scipy/pandas rather than trusted
as folklore.

## Basis

Calibrated against numpy 2.4.6, scipy 1.17.1 and pandas 3.0.3 — a corpus of
comments accepted as good — **not** against local history. Local history records
what was done, including past compromises, so it can measure a firing rate but
cannot say whether a target is bad.

Plain-comment calibration was then re-derived from 116 human labels (see
[`evaluation.md`](evaluation.md)). The reference corpora set a floor; the labels
showed the author's bar is stricter than mature scientific Python — 88 of 116
real committed comments were marked slop, and no plain comment that was kept
exceeded 30 words or two lines.

Doc prose has a separate ceiling. Dogfood showed that API contracts routinely
need a summary plus some implementation detail: useful retry and failure-state
docstrings occupied 29–80 words and 3–9 prose lines. Applying the plain ceiling
made artificial `Notes` sections the easiest way to preserve that rationale.
The doc ceiling leaves headroom around those examples but still reports the
next larger shared-helper explanation.

The ceiling is what buys that headroom, so the section no longer buys it too: a
`Notes` block or a `@remarks` tag is measured with the summary above it. Only
sections a convention shapes — parameter lists, returns, examples, `See Also`,
references — sit outside the budget. A 39-line docstring whose 19-line `Notes`
essay was judged on the 22 words above it is what made the difference concrete.

| constant | value | basis |
|---|---|---|
| `VERBOSE_WORDS` | 30 raw words | numpy p98 = 36, pandas p98 = 34, but no human-kept comment exceeded 30. At 35 only 6 of 46 labelled misses were caught |
| `VERBOSE_LINES` | 3 prose lines | no human-kept comment ran past two lines; 15 of 46 labelled misses were three lines or more |
| `DOC_VERBOSE_WORDS` | 90 raw words | allows the 80-word dogfood case with margin; a 91-word unstructured doc still fires |
| `DOC_VERBOSE_LINES` | 10 prose lines | allows up to 9 lines of bounded “how”; the next larger 10-line helper still fires |
| `DENSITY_MAX` | 30% | numpy p95 = 27.8%, pandas p95 = 25.9%, scipy = 33.3%; local added-regions p95 = 31.2% |
| `DENSITY_CEILINGS[.sh]` | 40% | shell has no function scope, so the ratio spans the whole script: vcpkg p95 = 38.5% over 89 scripts, PyBaMM = 37.1% over 54. Set above both, not between them |
| `DENSITY_MIN_COMMENT_LINES` | 3 | floor on comment volume, **not** region size — a region-size floor exempted a 5-line block on a 9-line change, the worst case there is |
| `MAX_WORDS` | 12 | gate for the opt-in `unreviewed` tier only |
| `LLM_DEADLINE` | 20 s | wall-clock budget for the whole sweep in hook mode |

The threshold sets are kind-specific, not language-specific, with `density` the
one exception: its ceiling is a percentile of a *scope*, and the scope differs by
language, so `DENSITY_CEILINGS` overrides it per extension. Python, TypeScript and
Rust API docs share one doc policy; all plain comments share the other. Python `#:` annotations had a third, intermediate policy; it was removed
because pairing a 60-word ceiling with density neutrality left a 58-word
annotation invisible to both tiers, and no reading of a `#:` block justified
that pair. Plain YAML and Rust thresholds remain provisional until their
labelled random samples settle them.

## `historical`: which "used to" is history

The tier's phrases are not all one construction, and the reading it wants is
the minority one. Every firing across the five corpora was read by hand — 107
blocks, of which `used to` was 63 and the past adverbs 26.

**`used to`.** The past habitual narrates the source ("this used to return a
bare array", "they used to fall off the end of the loop"); the reduced passive
states the contract ("the sparsity pattern used to create this coloring", "a
name that can be used to target this queue", "reference values used to generate
the data"). The passive was **50 of the 63**. A pronoun or demonstrative
subject is the one antecedent the passive never takes, so the tier requires it.

That is a deliberate recall trade, priced with the count in hand: **7 of the 13
real history findings are now missed**, all of them noun-subject ("Several
writers used to keep scanning"), which is word-for-word the shape of "the
Tableau used to create the solver". No lexical rule separates those two, and a
verb list is the classifier this repo already retired. The trade is 7 misses
against 50 false positives, in a tool whose stated priority is precision.

**The adverbs.** `previously` and friends have the same split, but the marker is
after the adverb, not before it: a participle plus an agent or means phrase
("previously returned by this library", "should have been previously initialised
using the output") or an attributive hyphen ("previously-picked template") states
runtime order rather than source history. Two lookaheads cover **16 of the 19**,
with no true finding reading that way. Still missed: a trailing `called
previously.` and an attributive `the previously installed skills`, both of which
need the comma or the following noun to be a part of speech a regex cannot see.

Net across the corpora: the tier went from 107 firings to 33, and 63 blocks
stopped being reported at all. The 116 human labels scored identically before and
after — 93% precision, 85% recall, the same six false positives, **the figures as
they stood at that change** — which is the evidence that the trade cost nothing
the ground truth can see. The headline is 95%/85% with four false positives now;
see [evaluation.md](evaluation.md) for the current number and never read a
figure in this section as the current one.

## `benchmark`: which number is a snapshot

A measured performance figure is true of one machine, one dataset and one day,
and nothing re-checks it. The PR body timestamps it and reads it as evidence for
a decision; the source cannot, so a reader has no way to tell a stale number
from a current one. The durable structural reason stays in the comment — only
the numbers move.

**The unit discriminates, not the digit.** `SPECIFICS` already treats a digit as
evidence a comment earns its place, and it is right to: a unit (`in
milliseconds (5 minutes)`), a limit (`PostgREST caps at 1000`) and a
non-latency percentile (`numpy p98=36`) are all durable. So the tier fires only
on a magnitude that carries a **time or size unit**, or one framed as a
**comparative gain**. A bare ratio stays out — `capacity ~2x the coulomb-count
estimate` is the domain, not a benchmark, and is the only shape of its kind
across the six corpora.

Two asymmetries are deliberate:

- **Duration is always a snapshot**, whatever verb reports it, so the verb list
  is wide (`took`, `costs`, `runs in`, `measured`). **Volume often is not** —
  "the header costs 2 bytes" is a format fact — so a size figure needs an
  explicit measurement verb (`measured`, `benchmarked`, `profiled`).
- The lookbehind on `MAGNITUDE` drops the `2x` inside an instance type
  (`m8i.2xlarge`), which is how the shape appears in the YAML corpus.

**A cost that carries an argument is a premise, not a snapshot.** The first
evidence the tier produced was against itself: pointed at this hook's own source
it fired twice, on `Costs 1.3 ms/file` and on `costs ~70s`. Both are the reason a
design decision is affordable — the first justifies a second parse path, the
second justifies the sweep being off by default — and deleting the number deletes
the argument. That is the opposite of what the tier is for, and it is the same
distinction the section above already draws: the durable reason stays, only the
free-floating number moves. So `benchmark` now defers to `REASON` and to a small
`BENCHMARK_PREMISE` set (`which is why`, `hence`), which keeps "Sampling costs 40
ms per file, which is why it is off by default" and "takes 30 minutes on a cold
cache, so it runs nightly" while `measured 812ms on an M2`, `~144x fewer buffers`
and `98.2% smaller` still fire.

**Evidence status: precision by construction, not by measurement.** The genre is
absent from the labelled corpus — the tier changes the base score by nothing
(95%/85%, tp 75, fp 4, fn 13, unchanged) — and nearly absent from committed code:
the only hits across the firing-rate populations are four `# measured baseline:
~138 MB` import-footprint annotations in the packages, which are exactly the
genre. Flood risk is nil and so is corpus evidence of precision. That is expected
rather than reassuring: the hook fires at write time, and a benchmark snapshot is
what an agent writes in the minutes after running one, which is exactly the
population no committed corpus samples. It is therefore the tier most in need of
the dogfood window in [`evaluation.md`](evaluation.md), and the one whose
reversion should be quickest if it misfires. `test_review_fixes.py` pins the
premise/snapshot split and asserts the hook no longer flags itself.

The one collision with an existing tier is recorded in
`test_a_premise_falls_through_to_the_length_fallback`, and the premise guard
settled it the other way. A seven-line `#:` block reading `measured 2ms,
computed 4.15ms and cost 9.30ms of wall per task` looks like the genre, but those
numbers *are* the argument for the threshold the block sits on — "move them to
the PR" deletes the reasoning, while `verbose`'s "shorten it" is the right advice
for seven lines. It fires as `verbose`.

## `unbuilt`: code that never reached `main`

`historical` covers what the code used to do. This covers what the code *never
did* — a change made and unmade on the same unmerged branch. The distinction
matters because the usual defence of a history comment does not apply: for a
merged change a reader can at least run `git log`, but a branch that was
rewritten, squashed or abandoned leaves nothing to resolve the reference
against. "We removed the retry wrapper" names something no reader can find.

**A broad "absent code" rule is unusable, and the width was measured rather
than chosen.** The obvious formulation — flag comments about removed, replaced
or no-longer-present code — was tested across 103,364 blocks (this repo's
backend, packages and frontend; plus numpy 2.4.6, scipy 1.17.1, pandas 3.0.3):

| candidate arm | firings | new findings | verdict |
|---|---:|---:|---|
| `no longer` | 154 | 82 | rejected |
| `is gone` / `was removed` / `no longer exists` | 53 | 23 | rejected |
| pronoun + past-tense removal verb | 5 | 2 | rejected |
| `never landed` / `never shipped` | 2 | 2 | rejected |
| **first-person edit verb** | **1** | **0** | kept |
| **considered-and-rejected** | **0** | **0** | kept |

The two rejected majority arms fail for one reason: **`no longer` almost always
states a present condition, not a history.** "The deleted row can no longer
satisfy the activity log's project FK" is a runtime invariant, "no row means
the channel is gone" is a current condition, and "unwatching something you no
longer watch is still success" is a contract. Deleting any of them deletes
load-bearing logic. That is the same shape as the passive `used to` in the
section above, and it resolves the same way: the phrase is not the signal.

`never landed` is rejected on its own evidence — both firings were about *data*
that failed to arrive ("an upload that never landed would still create a DB row
pointing at nothing"), and none about code.

**What survives is the first person past tense**, because it cannot describe
what a program does. "We removed the retry wrapper" has the author as subject;
"we tried to drop all levels" (pandas `multi.py`) has the program as subject,
which is why `tried` and `replaced` are excluded from the edit-verb arm and
appear only in the considered-and-rejected arm, where a `but … did not help`
clause disambiguates them.

**An edit that carries a consequence is a premise, not narration.** The single
firing across both corpora was pandas' `generate_legacy_storage_files`: "if we
have cases where we changed the signature (e.g. we renamed offset -> freq in
Timestamp), then we have to conditionally execute". The rename is *why* the
script branches, so deleting it deletes the argument — the same distinction
`benchmark` draws, and it is settled the same way. `REASON` alone does not
cover it (the case says "have to", not "must"), and widening the shared set
would move four other tiers, so the guard is a local `UNBUILT_PREMISE`:
obligation (`have to`, `needs to`), consequence (`which is why`, `hence`,
`so that`) and a conditional frame (`if we have …`). With it, the tier is at
**zero false positives across all 103,364 blocks**.

**Evidence status: precision by construction, not by measurement** — the same
standing as `benchmark`, and for the same reason. The tier adds zero firings to
either corpus and leaves the labelled score untouched (95%/85%, tp 76, fp 4,
fn 13, the same four false positives). A committed corpus cannot price this
genre, because a comment about a change made and unmade on a feature branch is
deleted, rewritten, or squashed before it is ever committed. The hook fires at
write time, which is the only moment that population exists. It should be the
quickest tier to revert if it misfires, and `test_comment_slop.py` pins both
the two arms and the four shapes that must stay silent.

## Reference percentiles moved, and the thresholds did not

`recalibrate.py` had its own extraction and its own density arithmetic, and both
disagreed with production. It rebuilt Python comments with `lstrip("#")`, which
left a `#:` body starting with `:` and dropped it from the word percentiles that
production classifies; and it counted every comment token in the density ratio,
including the `NEVER`-vetoed lines and the trailing comments production counts as
code. On a function with three `noqa` lines over nine statements it read 23% where
production read 0%. Every figure it produced was a percentile of a population the
hook never sees. It now calls `cs.extract`, `cs._group_blocks`, `cs.classified_*`
and `cs.density_sets` — the functions the hook itself calls — and keeps only its
`ast` pass for function extents, because production measures a diff hunk and the
reference scope has to be comparable.

Re-measured against the same numpy/scipy/pandas corpora:

| corpus | files | blocks | med | p95 | p98 | ≥5L | dens p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| numpy | 145 | 2,352 | 7 | 30 | 38 | 2.2% | 27.8% |
| scipy | 582 | 9,736 | 8 | 37 | 57 | 4.8% | 33.3% |
| pandas | 264 | 6,667 | 8 | 27 | 35 | 1.5% | 25.9% |
| combined | 991 | 18,755 | 8 | 32 | 47 | 3.3% | 29.4% |

Density came **down**, as it must: trailing comments left the numerator and vetoed
ones left it too. Read literally, the rule at the top of this file ("`DENSITY_MAX`
from the density p95 of the tightest corpus") now points at pandas' 25.9%.

**It is staying at 30%, and that is the disciplined answer, not the lazy one.**
The corpus holds **no labelled density cases** — `evaluation.md` says so, and the
snapshot score is a per-block tier measurement only. Lowering the bar makes the
tier fire more with nothing to check the new firings against, which is the move
this file exists to prevent. `VERBOSE_WORDS` stays at 30 for the same reason in
reverse: it was lowered from 35 *on 116 human labels*, and labels outrank a
percentile. Both numbers change when there is evidence about the firings they
would add, not because a reference corpus shifted.

## Firing rates

The fraction of real comment blocks the tiers flag.

**A band is a flood alarm, not a quality gate.** Precision and recall need
labels; firing rate does not, and it catches the failure labels reveal only
later — flooding a language until the hook gets muted. Never quote a band as
evidence of precision. Bands are enforced in `tests/test_firing_rate.py` at
+/-8 points.

| corpus | plain | doc |
|---|---|---|
| Python (private app corpus) | 21.8% | 5.2% |
| TS/TSX (private app corpus) | 38.6% | 6.0% |
| YAML (private app corpus) | 37.0% | — |
| Rust (diffsol) | 18.5% | 9.6% |
| Dockerfile (all local repos) | 16.9% | — |
| Shell (private app corpus) | 32.0% | — |

`tests/test_firing_rate.py` enforces these same figures, so that file and this
table are re-measured together. `dockerfile plain` is the one row a reader cannot
reproduce: `REPOS` is whatever the machine running it has checked out, and it
reads 11.5% here against the 16.9% recorded.

The bands **do** sample (a strided 400 files per language), and that is correct
here in a way it was not for the differential: a firing rate is a proportion, and
sampling estimates a proportion. The differential asks whether a disagreement
exists anywhere, which sampling can only be lucky about — see
[internals.md](internals.md).

The doc figures cover 3,685 Python, 633 TypeScript and 816 Rust blocks. Their
drop against the plain rates is the intended removal of length-only false
positives, not evidence of precision; edit narration, history, process leakage
and the other semantic tiers still run under the larger ceiling.

Most of the drift between one recorded table and the next is the corpora moving
on, not a tier changing — which is why the band is +/-8 and not +/-1. Two results
from re-measuring across tier changes outlive the numbers they produced:

- **Rust doc holds at 9.6% only because the merge exemption is scoped to PLAIN.**
  Applied to doc blocks it split `///` runs on their `# Safety` headings, nearly
  doubled the block count and took the rate to 4.5%, judging the prose under a
  heading out of the doc it belongs to. A `///` run is one comment in the
  language; a `#` run is separate comments we choose to read as a paragraph, and
  only the second is ours to split.
- **A veto is worse than a wrong tier**, so an over-matching marker costs more
  than a band can show. The comment is dropped before any tier sees it *and* it
  splits its block, so its neighbours are measured as a shorter run than the
  author wrote — and no corpus can see the class, because a vetoed comment is
  never eligible for sampling. Making the intent markers uppercase-only (see the
  README's *Tuning*) raised every band 0.3–1.0 points and left every labelled
  case scoring identically.

YAML is the outlier and the reason the thresholds stay provisional. Its comments
are distributionally longer than any other language's: reference p98 is **97
words** against 21 for Rust and 34–36 for numpy/pandas, with a median of 20.
Against the plain `VERBOSE_WORDS` of 30, essentially all of YAML's firing is the
length tiers reacting to a property of the format rather than to slop.

Dockerfile is the quietest plain corpus at 884 blocks, and no tier is
suppressed for it. The class to watch is `process-leak` and `step-marker` on
stage numbering: 50 of its 143 findings, but only **9 distinct comments**
repeated across checkouts, and mixed — `Stage 1: Build` over a `FROM ... AS
builder` is restatement, while `Step 1: PyPI deps — cached until uv.lock
changes` states the layer-ordering reason. Too few and too mixed to suppress on;
label them before acting.

Shell fires at 31.0% over 358 blocks, and none of it is `density`: that tier is
regional, so it never reaches a band, and the shell ceiling above was derived
from a separate measurement. What fires is `verbose` — 73 of this repo's 107
shell findings — and reference practice says the ceiling is right rather than
generous: 30 words / 3 prose lines trips **1.9%** of 1,738 blocks across vcpkg,
PyBaMM and casadi (p98 = 26 words), against 23.6% of this repo's own scripts. The
tier is reading a house habit, not the format.

If a plain genre floods, label its corpus and re-derive with
`recalibrate.py --lang` rather than guessing a new number.
