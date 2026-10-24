# Evaluation

`eval/` lives in the monorepo, not in the published package: its corpora quote
their sample source verbatim and that source is private. This page documents it
for a monorepo checkout.

`eval/` exists because an accuracy figure measured against an authored corpus
says little about a real diff: one such 99% reads 15% on one. The labels here are the only ground truth that does not come from the same
head that wrote the rules.

## Current numbers

**91% precision, 77% recall** over 121 labels (tp 71, fp 7, fn 21), scored
from the immutable comment lines captured with each case. A finding counts only
when it asks for the fix its label names: deletion for `slop`, shortening for
`trim` (see [Labels](#labels)).

Counted instead as flagging a comment the labeller would change, whatever fix it
asks for, the same findings are 100% precise at 85% recall (78 of 78, 78 of 92).
The difference is seven wrong fixes. Six are `verbose` asking to shorten a
`slop` comment: length cannot tell a long comment with nothing to keep from one
with a fact in too many words. The seventh is base#51,
`Test 2: Filter with x_max only`, labelled `trim` and deleted, because three words
are left once the marker goes.

Most of the move from 78%/70% (tp 62, fp 18, fn 27) is a change to the ground
truth, not to the detector. Sixteen labels were rejudged under the three-label
codebook, each with its reason in `eval/label-corrections.md`, and on their own
they take the previous detector to 89%/77%. The detector's share is two false
positives: base#42 `State` and base#92 `Step condition state`, each a heading
over a group of fields, which the heading rule now spares. In a monorepo
checkout, `uv run python eval/review_queue.py` lists the remaining disagreements
for re-review; `--blind` hides the old and detector verdicts.

Precision is the figure that matters: a false positive spends the author's trust,
a miss costs nothing.

**What that figure does not cover: trailing comments.** `generate_corpus.py` skips
inline comments when it samples — the `--llm` sweep never judges one, so the
corpus matches it — while the deterministic tiers do judge them. Unsuppressed, the
terseness tiers produce 330 findings on trailing comments across 2,212 files of
this repo, 133 of them `bare-identifier` on annotations like
`timeout = 5  # seconds`, and the corpus sees none of them. Those tiers are
suppressed inline (see the README's tier table for which), and density counts a
mixed line as code. This is a **structural** gap in the measurement, not a bug in
it: any tier scoped to a comment's position is invisible to a corpus sampled by
position. Sampling an inline corpus is the way to close it, and it has **not** been
done — deliberately. An unlabelled corpus is not evidence, and committing a sixth
one would be the mistake `test_corpus_schema.py` exists to stop. Generate
it when someone is ready to label it:

    cd eval && uv run --no-project python generate_corpus.py --genre <inline-genre> ...

which needs a `GENRES` entry that keeps inline comments instead of skipping them,
and the `--llm` exclusion left alone (the sweep still should not judge them).
Until then the honest reading of these figures is "over own-line comments".

The 39 remaining cases in `eval/corpus.txt` are unlabelled. Labelling them takes
TypeScript from 27% to 39% of the ground truth and replaces that baseline. The
base corpus is 160 cases, 121 labelled: **88 py / 21 tsx / 12 ts** — so the
headline figure is about three-quarters a Python measurement.

## Labels

| verdict | meaning | a finding is right when it asks to |
|---|---|---|
| `slop` | should not exist in any form | `delete` |
| `trim` | carries a fact worth keeping, in more words than the fact needs | `shorten` |
| `keep` | leave it as written | — |
| `skip` | cannot tell from what is shown | not scored |

`trim` exists because the two fixes are not interchangeable. Deleting a comment
that carries a real fact loses the fact; shortening one that carries nothing
leaves it in place. So every finding says which it asks for, and `score.py`
reads that action from the finding rather than guessing it from the tier:

- `verbose` asks to shorten.
- A tier objecting to part of a comment (a history clause, a `Step 1:` marker, a
  `file.py:12` pointer) asks to shorten, naming the part to cut, when what is
  left stands as a comment, and to delete when it does not. The README's
  *Delete or shorten* has the rule.
- Every other tier objects to the whole comment and asks to delete.
- The model sweep reports what it would not keep, which is a deletion.

Every slop and trim case is a positive. A finding asking for the other fix counts
twice, as a false positive and as a miss, in either direction, so precision's
denominator is every finding and recall's is every positive.

Labels entered under the two-label scheme filed "a long explanation where a
short one would do" as slop. One blind relabel re-judged the 73 `verbose`
findings under the three labels, and base#51 carries its blind verdict, `trim`.
Before the focused corrections, **160 of the 232 labels** (80 of the base
corpus's 121) still used the earlier standard. Sixteen have since been
rejudged; the rest still need a broad blind review. The six
remaining `verbose` findings on `slop` are the first action disagreements to
settle before tuning that tier.

### Rulings

The labeller's own reasons settle three cases the table leaves open:

- **A true secondary fact can still be trimmed.** Keep the fact the reader needs
  at that line. A parenthetical aside, a second paragraph of elaboration, or a
  restatement of what the code does makes the comment `trim`, even when every
  sentence is accurate. "Every sentence carries a fact" is not a reason to keep.
- **A hardcoded file path is a liability, not a fact.** It goes stale when the
  file moves, and a reader can search for the name. A path is a `trim` target;
  the rest of the comment is judged without it, and is `slop` if nothing is left.
- **A fact in the wrong place is judged as if it were moved.** A constraint that
  belongs in a type, a docstring or the docs does not keep the comment: `slop`
  if that fact is all it carries, `trim` if something else stays.

The labelling codebook settles three more, which the tiers follow:

- **A heading over a group is kept.** `State` over six fields, or `Robertson`
  over a benchmark block, names what is below it. One paraphrasing the single
  statement or definition it sits on is slop, and so is one naming that
  definition with its kind: `Cleanup function` over `cleanup() {`.
- **A formula, matrix, table or worked example is content**, never `trim` for
  its length, so its rows count toward neither of `verbose`'s limits.
- **A banner or step number over real content is `trim`**: the decoration goes
  and the content is judged on its own, which is what a cut does.

### `verbose`, out of sample

`verbose`'s precision was first measured on labels written while its findings
were on screen. The blind relabel re-judged the 55 `verbose` findings the
labeller had not been shown, and marked all 55 slop or trim. With the seven
findings already labelled keep, that is 55 of 62, against 66 of 73 in-sample.
None of the ten comments just under the threshold came back trim, so the
threshold sits where the labeller's shortening begins.

The figure rests on one person's standard of length. Two model annotators,
given a written codebook, kept 47 and 48 of the same 55: they keep a comment
when every sentence carries a fact, where the labeller shortens one whose
secondary facts (a parenthetical aside, a second paragraph) could go. No
written rule yet reproduces that standard, so a second human labeller is the
test this figure has not had.

## Scoring

    cd eval && uv run --no-project python score.py                    # tiers
    cd eval && uv run --no-project python score.py --llm              # tiers + sweep
    cd eval && uv run --no-project python score.py --genre yaml --channel random

Reports precision and recall against the labels, how many findings on a slop or
trim case asked for the other fix, and every false positive in full, with the
tier and action of a wrong fix. Partial labelling works.

`score.py` defaults to the captured `body_lines` and following code. This is the
classification input rather than a reconstructed source file: it preserves
physical span without requiring a partial Python fragment to parse, and it does
not drift when the source repository later gains lines or edits the file.

Every corpus carries `body_lines`, and `tests/test_corpus_schema.py` asserts that
plus the fields `snapshot_verdict` reads, the `body`/`body_lines` agreement, and
the `### n` numbering lining up with the JSON. A corpus without `body_lines` stops
snapshot scoring at the first verdict entered, and the gap stays invisible while
the corpus sits at 0% labelled, because an unlabelled corpus never exercises its
own schema. Regenerate with
`generate_corpus.py`, never by hand.

`--live` is the diagnostic path that reads the current file at the case's
recorded line number. Do not quote that number as a baseline: it changes with the working
tree. The sampled corpus contains no `density` cases, so the snapshot score is a
per-block tier measurement.

`--live` scores each case over its own line range, which for a comment case is
comments-only. Without the comment-only fallback (see *density* in
`internals.md`) that range measures 100% by construction, and `density` would
fire on every such case whatever the comment said — 10 of 10 on this repo's own
multi-line blocks. The snapshot score never calls `density`.

With `--llm`, deterministic findings are removed before model evaluation and the
remaining cases are grouped by file, matching the hook rather than batching
unrelated comments across repositories. `--show-errors` prints false negatives
as well as false positives.

Set `COMMENT_SLOP_EVAL_REPO` if the base corpus root differs from the default.

## Adding a genre

    cd eval && uv run --no-project python generate_corpus.py --genre yaml --root ~/repo -n 40

Genres: `py-docstring`, `yaml`, `ts-block`, `rust`, `rust-doc`,
`rust-pybamm`, `rust-pybamm-doc`, `shell`. `ts-block` and `rust-doc` have **no
committed corpus**: an unlabelled corpus is not evidence (above), so neither is
committed until someone is ready to label it. The seed is recorded in
`generate_corpus.py`.

A genre is one line in `GENRES` (extensions plus the kind to keep), and
`tests/test_corpus_schema.py` reads that table rather than a list of its own,
so a genre sampled as plain and scored as doc fails a test instead of scoring
quietly wrong.

Two genres may share an (extensions, kind) pair and differ only in what they
sampled. `rust`/`rust-doc` draw from diffsol and `rust-pybamm`/`rust-pybamm-doc`
from our own `pybamm-rust`, because the two answer different questions: the
first is reference practice by an author who is not us, the second is whether
the tiers misfire on code we wrote. Pooling them would give a precision figure
that does not say which of the two it measured, so the stem is what separates
them and neither is quoted for the other.

One eligible population, two independent draws, **never quoted together**:

- **random** — uniform over the whole population via a recorded seed. Comparable
  to the base corpus; headline precision/recall comes from here.
- **boundary** — an independent draw enriched for the decision boundary (word
  count or span within 20% of a threshold, or a tier fires). Shows worse
  precision *by construction*; for tuning only.

A case can be in both. `channel` and `seed` live in the JSON only — `corpus.txt`
reveals neither, nor which rule fires, so labels stay unanchored to what is being
measured.

The two draws are independent on purpose. Partitioning the population by detector
output — anything a tier flags is "boundary", the rest "random" — would condition
the random slice on the detector *not* firing and leave it unable to contain a
positive.

## Sampling caveats

Sampling deliberately over-represents long and multi-line blocks, and is not
gated on `model_eligible`, which excludes anything containing `because` /
`so that` / digits / parentheses. Long explanatory comments almost always contain
one of those, so the gate silently filters out nearly every long comment: a gated
sample of 150 is 129 single-line cases, the longest 35 words, and cannot test
`verbose` at all. That gate is right for evaluating the sweep — it is what
reaches the model — and wrong for evaluating the tiers.

**Authorship caveat for upstream corpora.** The Rust corpora sample crates this
project did not write. Those labels measure the labeller's taste applied to
someone else's code, not this codebase's conventions. Worth collecting, but
upstream labels alone must not move a threshold.

## Dogfood window

The per-genre precision gate is dogfood evidence rather than an upfront corpus
campaign: the hook fires on freshly added comments in real diffs, which is the
distribution live fires measure directly, and every fire is visible in the
session that produced it.

`eval/dogfood.md` holds the protocol, the per-genre reversion rule, and every
fire judged wrong. It is a hand-written log — nothing is collected automatically.

Reversion rule, per doc genre: three or more clear false positives **and** a
majority of that genre's fires judged wrong reverts the genre to the exemption.
A reversion is a legitimate outcome, not a failure. Flooding in a plain genre
points at the provisional thresholds instead — see
[`calibration.md`](calibration.md).

**Dogfooding cannot see misses.** A wrong fire self-reports; slop that should
have fired and did not is invisible. Recall claims therefore still require
labels, and none are made from dogfood evidence.

| genre | decision |
|---|---|
| py-plain, py-doc, ts-plain, ts-doc, yaml, rust-plain, rust-doc, rust-pybamm, rust-pybamm-doc | window open, no decision recorded yet |
| shell | `density` has its own 40% ceiling from reference measurement; per-comment tiers are shared |
