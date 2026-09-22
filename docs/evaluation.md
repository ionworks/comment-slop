# Evaluation

`eval/` lives in the monorepo, not in the published package: its corpora quote
their sample source verbatim and that source is private. This page documents it
for a monorepo checkout.

`eval/` exists because every accuracy figure produced before it was measured
against an authored corpus, and one of them (99%) turned out to be 15% on a real
diff. The labels here are the only ground truth that does not come from the same
head that wrote the rules.

## Current numbers

**95% precision, 85% recall** over 121 labels (tp 76, fp 4, fn 13), scored
from the immutable comment lines captured with each case.

Precision is the figure that matters: a false positive spends the author's trust,
a miss costs nothing.

**What that figure does not cover: trailing comments.** `generate_corpus.py` skips
inline comments when it samples — the `--llm` sweep never judges one, so the
corpus was built to match — and the deterministic tiers were judging them anyway.
Across 2,212 files of this repo that produced 330 findings on trailing comments,
133 of them `bare-identifier` on annotations like `timeout = 5  # seconds`, and
the corpus could not see a single one. The tiers whose signal is terseness are now
suppressed inline (see the README's tier table for which), and density counts a
mixed line as code. This is a **structural** gap in the measurement, not a bug in
it: any tier scoped to a comment's position is invisible to a corpus sampled by
position. Sampling an inline corpus is the way to close it, and it has **not** been
done — deliberately. An unlabelled corpus is not evidence, and committing a sixth
one would repeat the mistake `test_corpus_schema.py` was written to stop. Generate
it when someone is ready to label it:

    cd eval && uv run --no-project python generate_corpus.py --genre <inline-genre> ...

which needs a `GENRES` entry that keeps inline comments instead of skipping them,
and the `--llm` exclusion left alone (the sweep still should not judge them).
Until then the honest reading of 95% is "over own-line comments".

The 39 remaining cases in `eval/corpus.txt` are unlabelled. Labelling them takes
TypeScript from 27% to 39% of the ground truth and replaces that baseline. The
base corpus is 160 cases, 121 labelled: **88 py / 21 tsx / 12 ts** — so the
headline figure is about three-quarters a Python measurement.

## Scoring

    cd eval && uv run --no-project python score.py                    # tiers
    cd eval && uv run --no-project python score.py --llm              # tiers + sweep
    cd eval && uv run --no-project python score.py --genre yaml --channel random

Reports precision and recall against the labels and prints every false positive
in full. Partial labelling works.

`score.py` defaults to the captured `body_lines` and following code. This is the
classification input rather than a reconstructed source file: it preserves
physical span without requiring a partial Python fragment to parse, and it does
not drift when the source repository later gains lines or edits the file.

Every corpus carries `body_lines`, and `tests/test_corpus_schema.py` asserts that
plus the fields `snapshot_reason` reads, the `body`/`body_lines` agreement, and
the `### n` numbering lining up with the JSON. The five per-language corpora were
first committed without `body_lines`, so snapshot scoring refused to run on any of
them from the first verdict entered — invisible while they sat at 0% labelled,
because an unlabelled corpus never exercises its own schema. Regenerate with
`generate_corpus.py`, never by hand.

`--live` retains the old diagnostic path that reads the current file at its old
line number. Do not quote that number as a baseline: it changes with the working
tree. The sampled corpus contains no `density` cases, so the snapshot score is a
per-block tier measurement.

`--live` scores each case over its own line range, which for a comment case is
comments-only. Before the comment-only fallback (see *density* in
`internals.md`) that range measured 100% by construction, so `density` was
returned for every such case whatever the comment said — 10 of 10 on this
repo's own multi-line blocks. Live figures from before that change credited the
tier for cases it had not discriminated. The snapshot score is unaffected: it
never called `density`.

With `--llm`, deterministic findings are removed before model evaluation and the
remaining cases are grouped by file, matching `scan()` rather than batching
unrelated comments across repositories. `--show-errors` prints false negatives
as well as false positives.

Set `COMMENT_SLOP_EVAL_REPO` if the base corpus root differs from the default.

## Adding a genre

    cd eval && uv run --no-project python generate_corpus.py --genre yaml --root ~/repo -n 40

Genres: `py-docstring`, `yaml`, `ts-block`, `rust`, `rust-doc`,
`rust-pybamm`, `rust-pybamm-doc`, `shell`. `ts-block` and `rust-doc` have **no
committed corpus** — both were sampled, neither was labelled, and an unlabelled
corpus is not evidence (above), so they were removed. Re-sample either when
someone is ready to label it; the seed is recorded in `generate_corpus.py`.

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

An earlier design partitioned the population by detector output — anything a tier
flagged became "boundary", the rest "random" — which conditioned the random slice
on the detector *not* firing and left it unable to contain a positive.

## Sampling caveats

Sampling deliberately over-represents long and multi-line blocks. A first attempt
gated the sample on `model_eligible`, which excludes anything containing
`because` / `so that` / digits / parentheses. Long explanatory comments almost
always contain one of those, so every long comment was silently filtered out: 129
of 150 cases were single-line, the longest was 35 words, and `verbose` could not
be tested at all. That gate is right for evaluating the sweep — it is what
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
| shell | `density` recalibrated to a 40% ceiling on reference measurement; per-comment tiers unchanged |
