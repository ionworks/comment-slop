"""Keep the tree-sitter differential out of runs that cannot satisfy it.

`COMMENT_SLOP_ORACLE_STRICT` is the honest signal for "this run installed the
grammars and can actually compare"; `just test-hooks-oracle` sets it. Collection
is the layer that holds for every invocation, including a bare `python -m pytest`
and a `just test-hooks` given its own path -- see docs/internals.md for why a
recipe default was not enough.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    """Unset COMMENT_SLOP_STRICT, or a session launched with it set would run
    every test that calls `main` or the wrapper in strict mode."""
    monkeypatch.delenv("COMMENT_SLOP_STRICT", raising=False)


collect_ignore = (
    []
    if os.environ.get("COMMENT_SLOP_ORACLE_STRICT")
    else ["test_oracle_differential.py"]
)
