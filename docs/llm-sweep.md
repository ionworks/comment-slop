# The `--llm` narration sweep

**Status: exploratory, off by default, and not in the hook command.** The
deterministic tiers and `density` are the parts calibrated against evidence.

    uv run --no-project python comment_slop.py --llm FILE

The sweep lives in `src/comment_slop_llm.py`, apart from the module the hook
reloads on every edit. It must sit beside `comment_slop.py`; a copy that vendors
the one file warns on `--llm` and reports the deterministic findings alone. It
plugs into `analyze` as its `sweep` argument, so it only ever sees what the
tiers left — in scope, not exempt, not already fired on.

## Why narration is not a tier

Narration ("Loop over every row" above `for r in rows:`) is the commonest slop,
and the hook does not detect it per-line. That is deliberate.

`restates-code` cannot reach it: it is a subset test, so it needs shared
vocabulary, and the commonest narration shares none — `row` vs `rows` is 0%
overlap, and `# Set up the accumulator` over `out = []` shares nothing at all.

A curated verb list works (~80% precision) but is unmaintainable: naming
which verbs count needs endless growing, and each round is guesswork. A POS
tagger cannot substitute — NLTK and spaCy both read `Configure` and `Loop` as
nouns in comment-initial position, spaCy's small model does not populate `Mood`
at all, and imperative mood does not separate the classes anyway (*"Guard against
sentinel rows the upstream feed emits"* is imperative and worth keeping).
Dropping the verb list and keeping only the exemptions fires on 2.39 comments per
file, which is the `unreviewed` failure again.

So the judgement is left to a model.

## Safety properties

Deterministic vetoes run first, so the model only ever sees comments that are not
lint directives, not attribute docs, and carry no stated reason or specifics. It
receives comment+code pairs and returns ids from the batch it was given, so it can
narrow that set and nothing else — it cannot invent a finding, and it never emits
code. An unreachable model or malformed reply yields the deterministic findings
alone.

Because the ask is inverted (see below), a failed batch is **skipped**, never read
as "keep nothing" — otherwise an unreachable model would flag every comment it was
about to judge. There is a test for exactly that.

`comment_slop_llm.classify()` takes a wall-clock deadline and reports how many
comments it did not send. A batch cap bounds the number of calls, not the time
they take: three batches against a 120 s timeout is a six-minute stall on a
single edit.

## A model finding asks for deletion

The model answers keep or not, so a comment it does not keep is reported as
`delete`, and `score.py` scores it that way. The prompt's seventh class, "a long
explanation where a short one would do", describes `trim` instead, whose fix is
a shorter comment, so a finding made on those grounds asks for the wrong fix.
The sweep only sees what `verbose` let through, which bounds how often that can
happen, but not by a measured amount.

Dropping the class, or asking for keep / shorten / delete, would align the two.
Either is a prompt change to measure against the table below before it lands,
because naming the classes is what carries the sweep's recall.

## Why it is not in the hook

Run over a real 28-commit branch (21 files, 161 added comment lines) the sweep
produced 9 model findings of which roughly **1–2 were true positives**. That is
not good enough to interrupt anyone with, and a model call is not latency-free
even when it is right.

The corpus was the problem, not the prompt. The original 99% was measured against
a **self-authored** corpus whose "good comments" were textbook — full sentences
stating a reason. Real good comments are terse, domain-specific, and often
annotate semantics rather than explaining: `# memory path never engaged` beside an
assertion, `# ready` beside a lambda. The model reads that terseness as narration.

Two mechanical fixes came out of it: inline comments excluded from the batch
(9 findings → 4), and a grouping gap where an inline comment followed by own-line
continuations was judged as a fragment.

Narrower prompts were then tried, on the theory that the model fails at open
judgement but succeeds at specific classification. Measured against 120 real
committed comments and 72 real tier-flagged ones:

| framing | false positives | recall |
|---|---|---|
| open judgement (current) | 37% | 64% |
| narrow yes/no | 67% | 50% |
| per-class label | **29%** | 53% |

Best case is 29% false positives on comments that were written, reviewed and
committed. Unusable. (An earlier 0/15 on a smaller sample was noise — a caution
about small evaluation sets, which is the whole reason `eval/` exists.)

It goes back into the hook when a labelled real-diff measurement justifies it.

## Production-shaped re-evaluation, 2026-07-30

The earlier scorer overstated the sweep in two ways: it sent comments already
caught by deterministic tiers, and it batched unrelated cases from different
files together. Production does neither. The corrected evaluator filters
deterministic findings first and batches per file.

On the 116 labelled snapshot. **One run, recorded before the `edit-narration`
vocabulary fix** — the rows are comparable to each other, not to today's headline,
which is 91%/77% at fp 7, scoring each finding's action against its label.
Re-run the whole table rather than editing one row into it.

| path | precision | recall | result |
|---|---:|---:|---|
| deterministic tiers (baseline for this run) | 93% | 85% | tp 75, fp 6, fn 13 |
| current keep-list sweep, production-shaped | 93% | 85% | added no findings |
| strict singleton remove-list prototype | 87% | 92% | delete-direction bias |
| fixed good/bad anchors per candidate | 92% | 91% | added 5 tp and 1 fp |

The reason is batch-relative judgement. Across five shuffled corpus-wide runs,
only 19 of 52 model findings were stable; recall ranged from 90–93% and
precision from 88–92%. With one residual candidate, the current inverted
keep-list prompt kept 22 of 23. Confidence was not useful: a singleton
decision/confidence prototype marked every residual `keep`, almost all at
confidence 3.

Fixed anchors made the anchor decisions stable but did not meet the 93%
precision floor on the held-out post-75 cases. No prompt or batching change from
this experiment ships. A future sweep needs either a model demonstrably capable
of absolute rather than batch-relative classification, or a larger independently
labelled holdout that supports a calibrated verifier.

## Model choice, measured

Scored on a 55-case labelled corpus, thinking off, three worked examples:

| model | accuracy | per batch of 12 |
|---|---|---|
| Qwen3.6-35B-A3B (MoE, 3B active) | **91%** | ~1.5s |
| qwen3:4b | 76% | 1.56s |
| qwen3:1.7b | 69% | 0.72s |
| gemma3:4b | 67% | 1.76s |
| qwen2.5-coder:3b | 54% | 0.91s |

The MoE model is both the most accurate and no slower than a dense 4B, because
only 3B is active. Small dense models are worse on both axes at once.

**Thinking off** is load-bearing: with reasoning enabled a batch costs ~70 s
instead of ~1.2 s, and on a small token budget it returns empty content.

## Prompt shape, A/B tested

On a 75-case labelled corpus, same model, thinking off:

| prompt | accuracy | slop recall | keep precision |
|---|---|---|---|
| generic delete/keep, 3 examples | 87% | 92% | 74% |
| + the seven target classes named | 84% | **100%** | **48%** |
| + balanced examples incl. hard keeps | 89% | 100% | 65% |
| **+ ask which to KEEP, not which to DELETE** | **97%** | 96% | **100%** |
| **+ 4 lines of code context** | **99%** | **98%** | **100%** |

Three things carry it, and the third is counterintuitive:

- **Name the classes.** Recall 92% → 100%.
- **Balanced worked examples**, including the hard keeps (a named invariant, an
  ordering constraint, a unit).
- **Invert the ask.** Naming the classes alone dropped keep-precision to 48% —
  the model over-generates whichever label it is asked for, so asking "which
  should be deleted" makes it delete-happy. Asking "which should be kept" took the
  identical prompt to 100% keep-precision.

## Configuration

| variable | default |
|---|---|
| `COMMENT_SLOP_API` | `http://127.0.0.1:8501/v1/chat/completions` |
| `COMMENT_SLOP_MODEL` | `local` |
| `COMMENT_SLOP_TIMEOUT` | `120` |
