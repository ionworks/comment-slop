"""Labelled detection corpus: does real slop get caught, and is good prose left alone?

Code coverage proves the branches execute. It says nothing about whether the
detector finds what it exists to find. This file is the standing guard on
detection quality: each case is a realistic comment plus the code it sits above,
labelled with whether it should be reported.

Aggregate recall and precision are asserted at the bottom, so a change that
trades one for the other fails here rather than silently degrading.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs

# Narration: only the --llm sweep reaches these; the hook sees them in aggregate
# via `density`, never individually.

# Kept as a corpus so the sweep can be scored and the gap stays documented.
NARRATION = [
    ("Set up the accumulator", "out = []"),
    ("Loop over every row", "for r in rows:\n    pass"),
    ("Skip empty rows", "if not r:\n    continue"),
    ("Append to the output", "out.append(r)"),
    ("Return the result", "return out"),
    ("Iterate over the objectives", "for o in objectives:\n    pass"),
    ("Check if the cache is populated", "if cache:\n    pass"),
    ("Create the response object", "resp = Response()"),
    ("Initialize the counter", "n = 0"),
    ("Build the query", "q = table.select()"),
    ("Parse the JSON payload", "data = json.loads(body)"),
    ("Sort the results", "results.sort()"),
    ("Raise a validation error", "raise ValueError('bad')"),
    ("Increment the retry count", "retries += 1"),
    ("Log the outcome", "logger.info('done')"),
    # verbs added after auditing real deletions: the first list was seeded from
    # one probe file and knew only loop/return/skip/append/create/check
    ("Configure logger", "logging.basicConfig(level=INFO)"),
    ("Use defaults from router configuration", "cfg = router.defaults"),
    ("Construct callback URL with fallback", "url = base + path"),
    ("Compute the mean voltage", "v = total / count"),
    ("Store the result in the cache", "cache[key] = value"),
    ("Clear the pending queue", "queue.clear()"),
    ("Filter out the archived rows", "rows = [r for r in rows if r.live]"),
    ("Prepare data for creation", "create_data = model.dump()"),
    ("Convert objectives", "config.update(parse(objectives))"),
    ("Format date values", "if col in DATE_COLS and value:\n    v = fmt(value)"),
    # A noun subject reads the same as the passive that states a contract, so
    # this history costs 50 false positives to catch. docs/calibration.md.
    ("the parser used to accept a bare string here", "parse(obj)"),
]

# (comment, following code) -- each should be reported by the hook alone
SLOP = [
    # lexical restatement
    ("sort items", "items.sort()"),
    ("close the connection", "connection.close()"),
    # edit narration
    ("Fixed: skip None entries", "if x is None:\n    continue"),
    ("Added a guard for the empty case", "if not xs:\n    return"),
    ("Updated to use the new client", "client = NewClient()"),
    ("Removed the redundant check", "value = compute()"),
    # measured gaps: the model caught these 6/6 while the regex managed 4/6,
    # so the closed vocabulary was less closed than assumed
    ("Reworked to avoid the extra round-trip", "cached = load()"),
    ("Tweaked the ordering here", "items.sort()"),
    ("Simplified the guard", "if not xs:\n    return"),
    ("Switched to the async client", "client = AsyncClient()"),
    ("Reverted the earlier change", "value = original()"),
    # structural noise
    ("===== TOTALS =====", "n = 0"),
    ("--- project ---", "project = get()"),
    ("-" * 60, "x = 1"),
    ("Helpers", "def helper():\n    pass"),
    ("Utilities", "def util():\n    pass"),
    ("done \u2705", "x = 1"),
    # process and history leakage
    ("Task 3: wire the reservation through", "wire()"),
    ("Step 2: upload the metadata", "upload()"),
    ("left unhandled in this PR; the caller validates", "skip()"),
    ("this previously asserted the ilike form", "assert q == exact"),
    ("broken on prod 2026-07-10 when the head node changed", "fix()"),
    ("this used to accept a bare string here", "parse(obj)"),
    # stale pointers
    ("mirrors the check in resource_plan.py:412", "guard()"),
    ("same as run_simulation.py lines 46-49", "run()"),
    # verbose: over the word cap, and a tall block of short lines
    (" ".join(f"word{i}" for i in range(40)), "run()"),
    ("alpha\n# beta\n# gamma\n# delta\n# epsilon", "run()"),
]

# (comment, following code) -- each must be left alone
GOOD = [
    # a reason, a tradeoff, or a constraint outside the file
    (
        "Guard against sentinel rows the upstream feed still emits with a zeroed "
        "timestamp rather than a null",
        "if r.ts == EPOCH:\n    continue",
    ),
    (
        "Return early: a cold cache means the row was never persisted, so a miss "
        "here is not an error",
        "return None",
    ),
    ("Validate uniqueness to prevent division by zero downstream", "check(values)"),
    (
        "Iterate in reverse so deletions do not shift the later indices",
        "for i in reversed(rng):\n    pass",
    ),
    (
        "Initialize to None rather than 0 so a missing reading stays "
        "distinguishable from a real zero",
        "x = None",
    ),
    (
        "Build the query with inner joins so the exact count stays correct under "
        "pagination",
        "q = table.select(count='exact')",
    ),
    (
        "The two must agree: the driver's worker-node ceiling has to match what "
        "was actually provisioned",
        "assert a == b",
    ),
    (
        "Function-level import: the module pulls in the pipeline package, which is "
        "expensive at process start",
        "from pkg import thing",
    ),
    (
        "A corrupt line should not discard the whole transcript",
        "except JSONDecodeError:\n    continue",
    ),
    (
        "Non-fatal: run the turn without preferences rather than failing it",
        "prefs = {}",
    ),
    (
        "Conditional, terminal-guarded write -- the durability guarantee",
        "if not terminal:\n    write()",
    ),
    (
        "Comparing a naive and an aware datetime raises, and mixed awareness is a "
        "malformed pair we do not judge here",
        "if a.tzinfo is None:\n    return",
    ),
    ("distinct point \u2192 measured warm solve", "solve()"),
    ("keyed by (job_id, organization_id, objective_name)", "cache = {}"),
    # specifics the code does not state: a parenthetical, a unit, a worked
    # example. These also rescue the noun reading of a narration verb.
    ("Cache duration in milliseconds (5 minutes)", "CACHE_MS = 5 * 60 * 1000"),
    ("Get raw bounds (could be None)", "lb, ub = self.get_bounds()"),
    ('Handle "NC" format (e.g., "3C" -> 3, "0.5C" -> 0.5)', "m = s.match(rx)"),
    (
        "Configure the logger before any import pulls in the pipeline package",
        "logging.basicConfig()",
    ),
    (
        "Use a per-call client, not the shared one: this runs on a Ray worker with "
        "its own event loop",
        "client = Client()",
    ),
    (
        "Only the current, non-aborted request owns the loading state",
        "if reqId == current:\n    setLoading(False)",
    ),
    # edit vocabulary appearing anywhere but the opening position
    (
        "Use a new client here because the shared one is bound to the wrong loop",
        "client = Client()",
    ),
    ("The move to keyset pagination keeps the offset math honest", "q = page(q)"),
    ("Splitting on the delimiter would break the quoted case", "parts = parse(s)"),
    # vetoed by convention
    ("noqa: E501", "x = 1"),
    ("type: ignore[arg-type]", "x = f(y)"),
    ("TODO: revisit once the migration lands", "x = 1"),
    ("SAFETY: the lock is held by the caller", "mutate()"),
    ("see https://github.com/x/y/issues/42", "workaround()"),
]


def fires(comment: str, code: str) -> bool:
    src = "".join(f"# {line}\n" for line in comment.split("\n# ")) + code + "\n"
    return bool(cs.find_findings(src, "a.py", None))


def test_deterministic_slop_recall():
    """What the hook must catch with no model."""
    missed = [c for c, k in SLOP if not fires(c, k)]
    recall = 1 - len(missed) / len(SLOP)
    detail = "\n".join(f"    MISSED: {m[:70]}" for m in missed)
    assert recall >= 0.90, f"recall {recall:.0%} of {len(SLOP)}\n{detail}"


def test_narration_is_not_reached_without_the_model():
    """Asserts the gap, so removing the sweep cannot silently lose the class."""
    caught = [c for c, k in NARRATION if fires(c, k)]
    assert len(caught) <= len(NARRATION) * 0.2, (
        "narration is being caught deterministically again; if a tier was added "
        f"back, move these cases into SLOP. Caught: {caught[:5]}"
    )


def test_good_comments_are_not_flagged():
    flagged = [c for c, k in GOOD if fires(c, k)]
    precision_proxy = 1 - len(flagged) / len(GOOD)
    detail = "\n".join(f"    FALSE POSITIVE: {f[:70]}" for f in flagged)
    assert precision_proxy >= 0.90, (
        f"{len(flagged)} of {len(GOOD)} good comments flagged\n{detail}"
    )


def test_corpus_is_balanced_enough_to_be_meaningful():
    """A recall figure over a handful of cases is not evidence."""
    assert len(SLOP) + len(NARRATION) >= 30
    assert len(GOOD) >= 15


def test_model_sweep_scores_well_when_a_model_is_reachable():
    """Skipped without a local model. Scores the sweep on the cases the
    deterministic tiers deliberately do not reach, plus the keepers."""
    import pytest

    pairs = [(i, c, k.splitlines()[0]) for i, (c, k) in enumerate(NARRATION, 1)]
    got = cs.classify(pairs)
    if not got:
        pytest.skip("no local model reachable")
    recall = len(got) / len(pairs)
    keep = [
        (i, c, k.splitlines()[0])
        for i, (c, k) in enumerate(GOOD, 1)
        if cs.model_eligible(c) and not cs.NEVER.search(c)
    ]
    wrong = cs.classify(keep) if keep else set()
    precision = 1 - len(wrong) / max(1, len(keep))
    assert recall >= 0.75, f"sweep recall {recall:.0%}"
    assert precision >= 0.60, f"sweep precision {precision:.0%} on keepers"
