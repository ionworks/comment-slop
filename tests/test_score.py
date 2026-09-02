"""The evaluator is an acceptance test, so its inputs must not drift."""

import json
from pathlib import Path
import sys

_PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG / "src"))
# `eval` is a namespace package under the package root, which is only on the
# path when pytest's rootdir happens to be that root. Say so instead.
sys.path.insert(0, str(_PKG))

import comment_slop as cs
from eval import score


def test_human_labelled_snapshot_does_not_regress():
    root = Path(__file__).resolve().parent.parent
    cases = json.loads((root / "eval" / "cases.json").read_text())["cases"]
    labels = score.read_labels(str(root / "eval" / "corpus.txt"))
    hit = {i: bool(score.snapshot_reason(cases[i - 1], genre=None)) for i in labels}
    tp = sum(labels[i] == "slop" and hit[i] for i in labels)
    fp = sum(labels[i] == "keep" and hit[i] for i in labels)
    fn = sum(labels[i] == "slop" and not hit[i] for i in labels)
    assert tp >= 75
    assert fp <= 6
    assert fn <= 13


def test_snapshot_reason_uses_captured_lines_not_the_working_tree():
    case = {
        "file": "missing.py",
        "line": 20,
        "span": 1,
        "body_lines": ["--- Tests for a descriptive integration scenario ---"],
        "body": "--- Tests for a descriptive integration scenario ---",
        "code": ["def test_it():", "    pass"],
    }
    assert score.snapshot_reason(case, genre=None) == "banner"


def test_snapshot_reason_preserves_doc_kind(monkeypatch):
    seen = []

    def capture(block, target=""):
        seen.append((block.kind, target))
        return None

    monkeypatch.setattr(cs, "block_reason", capture)
    case = {
        "line": 1,
        "body_lines": ["Summary."],
        "body": "Summary.",
        "code": ["def f(): pass"],
    }
    score.snapshot_reason(case, genre="py-docstring")
    assert seen == [(cs.DOC, "def f(): pass")]


def test_snapshot_reason_defaults_an_unmapped_genre_to_plain(monkeypatch):
    """`#:` annotations are plain, so no Python genre maps to a doc kind other
    than `py-docstring`, and the fallback is what carries the rest."""
    seen = []

    def capture(block, target=""):
        seen.append((block.kind, target))
        return None

    monkeypatch.setattr(cs, "block_reason", capture)
    case = {
        "line": 1,
        "body_lines": ["Field explanation."],
        "body": "Field explanation.",
        "code": ["VALUE = 1"],
    }
    score.snapshot_reason(case, genre="yaml")
    assert seen == [(cs.PLAIN, "VALUE = 1")]


def test_model_scoring_filters_tier_hits_and_batches_per_file(monkeypatch):
    calls = []

    def capture(pairs):
        calls.append(pairs)
        return {line for line, _, _ in pairs}

    monkeypatch.setattr(cs, "classify", capture)
    cases = [
        {"file": "a.py", "body": "one", "code": ["x = 1"]},
        {"file": "a.py", "body": "two", "code": ["x = 2"]},
        {"file": "b.py", "body": "three", "code": ["x = 3"]},
    ]
    got = score.flagged_by_model(cases, eligible={1, 3})
    assert got == {1, 3}
    assert [[item[0] for item in call] for call in calls] == [[1], [3]]
