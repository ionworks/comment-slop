"""The `--llm` narration sweep: ask a local model which comments to keep.

Exploratory and off by default -- see docs/llm-sweep.md. It lives apart from
`comment_slop` because that module reloads on every edit and none of this
runs there; vendoring the single file drops `--llm` and nothing else.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

import comment_slop as cs

LLM_API = os.environ.get(
    "COMMENT_SLOP_API", "http://127.0.0.1:8501/v1/chat/completions"
)
LLM_MODEL = os.environ.get("COMMENT_SLOP_MODEL", "local")
LLM_TIMEOUT = int(os.environ.get("COMMENT_SLOP_TIMEOUT", "120"))
LLM_BATCH = 12
# Wall-clock budget for the whole sweep. A batch cap bounds the number of
# calls, not the time they take: 3 batches x LLM_TIMEOUT is a six-minute stall.
LLM_DEADLINE = 20

# On a 75-case corpus, naming the target classes lifts recall from 92% to 100%
# but drops keep-precision to 48%.

# The model over-generates whichever label it is asked for, so asking which to
# KEEP rather than DELETE is what reaches 99%.

# Thinking stays off -- ~70s a batch against ~1.2s, for no gain.
LLM_CONTEXT_LINES = 4  # 1 line scored 97%, 4 lines 99%, at the same latency
LLM_PROMPT = """You review code comments and report which ones should stay.

A comment should NOT stay if:
1. It restates what the code plainly does ("Loop over every row" above a `for`).
2. It narrates the edit that was made ("Fixed:", "Reworked to", "Tweaked").
3. It labels an obvious section ("Helpers", "Utilities", "===== TOTALS =====").
4. It leaks process or planning ("Task 3:", "Step 2:", "in this PR").
5. It records history that belongs in git ("previously asserted", "used to accept").
6. It points at a line number that will rot ("resource_plan.py:412", "lines 46-49").
7. It is a long explanation where a short one would do.

A comment SHOULD stay if deleting it would lose a fact that cannot be recovered
by reading the surrounding code: the reason a choice was made, a non-obvious
constraint or ordering requirement, a named invariant, a footgun, a unit, a
worked example, or a pointer to something outside the file.

When uncertain, keep it. A wrongly deleted explanation costs more than a
surviving restatement.

Worked examples:
[1] // Loop over every row
    for r in rows:
-> not kept (the `for` already says it)
[2] // Fixed: skip None entries
    if x is None: continue
-> not kept (narrates the edit, not the reason)
[3] // Step 2: upload the metadata
    upload(meta)
-> not kept (plan step, belongs in the PR)
[4] // Guard against sentinel rows the upstream feed still emits
    if r.ts == EPOCH: continue
-> KEEP (why EPOCH is special is not in the code)
[5] // Conditional, terminal-guarded write -- the durability guarantee
    if not terminal: write()
-> KEEP (names the invariant the branch protects)
[6] // Configure the logger before any import pulls in the pipeline package
    logging.basicConfig()
-> KEEP (states an ordering constraint)
[7] // Cache duration in milliseconds (5 minutes)
    CACHE_MS = 5 * 60 * 1000
-> KEEP (supplies the unit and a human-readable value)

Return only JSON listing the ids to keep: {"keep": [ids]}"""


def classify(
    pairs: list[tuple[int, str, str]], deadline: float | None = None
) -> set[int]:
    """Ask the local model which comments are slop. Returns line numbers.

    The model is asked which comments to KEEP; anything in the batch it does not
    keep is reported. It only ever sees comment+code text and only ever returns
    ids from the batch it was given, so it cannot point at a line it was not
    shown and never emits code.

    A batch that fails is skipped entirely rather than treated as "keep
    nothing" -- with the ask inverted, an unreachable model would otherwise
    flag every comment it was about to judge.
    """
    out: set[int] = set()
    stop = None if deadline is None else time.monotonic() + deadline
    sent = 0
    for start in range(0, len(pairs), LLM_BATCH):
        if stop is not None and time.monotonic() >= stop:
            cs.warn(
                f"{len(pairs) - sent} comments not sent to the model "
                f"(deadline {deadline:g}s reached)"
            )
            break
        chunk = pairs[start : start + LLM_BATCH]
        sent += len(chunk)
        items = "\n".join(
            f"[{i}] // {body}\n" + "\n".join(f"    {line}" for line in code)
            for i, (_, body, code) in enumerate(chunk, 1)
        )
        payload = {
            "model": LLM_MODEL,
            "temperature": 0,
            "max_tokens": 320,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": LLM_PROMPT},
                {"role": "user", "content": items},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "r",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "required": ["keep"],
                        "properties": {
                            "keep": {"type": "array", "items": {"type": "integer"}}
                        },
                    },
                },
            },
        }
        req = urllib.request.Request(
            LLM_API,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            budget = (
                LLM_TIMEOUT
                if stop is None
                else max(1.0, min(LLM_TIMEOUT, stop - time.monotonic()))
            )
            with urllib.request.urlopen(req, timeout=budget) as resp:
                body = json.load(resp)
            text = body["choices"][0]["message"]["content"] or ""
            kept = json.loads(text[text.index("{") : text.rindex("}") + 1])["keep"]
        except Exception:
            cs.warn("local model unreachable; reporting deterministic findings only")
            continue
        keep_ids = {i for i in kept if isinstance(i, int)}
        out |= {ln for i, (ln, _, _) in enumerate(chunk, 1) if i not in keep_ids}
    return out


def model_eligible(body: str) -> bool:
    """Whether a comment is worth spending a model call on.

    REASON and SPECIFICS do the discriminative work a verb list would attempt,
    and they cheaply keep obvious keepers out of the batch. There is no verb
    list -- naming which verbs count is unmaintainable, and a POS tagger cannot help (taggers read "Configure" and
    "Loop" as nouns in comment-initial position, and imperative mood does not
    separate the classes anyway: "Guard against sentinel rows the upstream feed
    emits" is imperative and worth keeping).
    """
    return not (cs.REASON.search(body) or cs.SPECIFICS.search(body))


def sweep(deadline: float | None = None) -> cs.Sweep:
    """The `sweep` argument to `cs.analyze`, bounded by `deadline` seconds.

    It is handed the blocks no tier fired on. Own-line ones only: a trailing
    comment is terse by necessity, and the model reads that as narration -- 4
    of 9 false positives on one diff.
    """

    def run(residue: list[cs.Comment], lines: list[str]) -> list[cs.Finding]:
        pairs = []
        for b in residue:
            if b.inline:
                continue
            body = cs.classified_body(b)
            if not model_eligible(body):
                continue
            code = cs.block_context(lines, b, LLM_CONTEXT_LINES)
            if code:
                pairs.append((b.line, body, code))
        texts = {ln: body for ln, body, _ in pairs}
        return [
            cs.Finding(ln, "narration (model)", texts[ln])
            for ln in sorted(classify(pairs, deadline))
        ]

    return run
