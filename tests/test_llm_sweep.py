"""The `--llm` narration sweep in `comment_slop_llm`, and how it joins `analyze`."""

import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import comment_slop as cs
import comment_slop_llm as llm


def make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return tmp_path


def commit_all(repo: Path, message: str = "wip") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


class TestModelEligible:
    """REASON and SPECIFICS keep obvious keepers out of the batch before any
    model call is spent on them."""

    def test_model_eligible_skips_a_stated_reason(self):
        assert not llm.model_eligible("Validate uniqueness to prevent div by zero")
        assert not llm.model_eligible("Skip the rows because the feed repeats them")

    def test_model_eligible_skips_specifics(self):
        assert not llm.model_eligible("Cache duration in milliseconds (5 minutes)")
        assert not llm.model_eligible('Handle "NC" format (e.g. "3C" -> 3)')

    def test_model_eligible_admits_bare_narration(self):
        assert llm.model_eligible("Loop over every row")
        assert llm.model_eligible("Set up the accumulator")


class TestSweepFailureIsSilent:
    """A sweep that cannot reach a model reports the deterministic findings."""

    def test_unreachable_endpoint_yields_no_model_findings(self, monkeypatch, capsys):
        monkeypatch.setattr(llm, "LLM_API", "http://127.0.0.1:1/v1/chat/completions")
        monkeypatch.setattr(llm, "LLM_TIMEOUT", 1)
        assert llm.classify([(1, "Loop over every row", ["for r in rows:"])]) == set()
        assert "unreachable" in capsys.readouterr().err

    def test_malformed_response_yields_no_model_findings(self, monkeypatch):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not json"

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
        assert llm.classify([(1, "Loop over every row", ["for r in rows:"])]) == set()

    def test_model_only_addresses_lines_it_was_shown(self, monkeypatch):
        """The ask is inverted: ids are keeps, and anything unkept is reported.
        Ids outside the batch are ignored, so the model cannot point at a line
        it was never given."""
        import io as _io

        payload = {"choices": [{"message": {"content": '{"keep": [2, 99, -3]}'}}]}

        class FakeResp:
            def __enter__(self):
                return _io.BytesIO(json.dumps(payload).encode())

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
        got = llm.classify(
            [
                (11, "Loop over every row", ["for r in rows:"]),
                (22, "Guard against the sentinel feed", ["if x: pass"]),
            ]
        )
        assert got == {11}, "id 2 was kept; 99 and -3 are not in the batch"

    def test_failed_batch_is_skipped_not_read_as_keep_nothing(
        self, monkeypatch, capsys
    ):
        """With the ask inverted, treating a failure as an empty keep-list would
        flag every comment in the batch."""
        monkeypatch.setattr(llm, "LLM_API", "http://127.0.0.1:1/v1/chat/completions")
        monkeypatch.setattr(llm, "LLM_TIMEOUT", 1)
        got = llm.classify(
            [
                (11, "Loop over every row", ["for r in rows:"]),
                (22, "Return the result", ["return out"]),
            ]
        )
        assert got == set()
        assert "unreachable" in capsys.readouterr().err

    def test_sweep_off_makes_no_model_call(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("classify must not run without --llm")

        monkeypatch.setattr(llm, "classify", boom)
        f = tmp_path / "a.py"
        f.write_text("# Loop over every row\nfor r in rows:\n    pass\n")
        cs.scan(str(f))


class TestSweepIntegration:
    """What reaches the model, and what is filtered out before it."""

    @staticmethod
    def _capture(monkeypatch):
        seen = []

        def fake(pairs, deadline=None):
            seen.extend(pairs)
            return {ln for ln, _, _ in pairs}

        monkeypatch.setattr(llm, "classify", fake)
        return seen

    def test_narration_becomes_a_model_finding(self, tmp_path, monkeypatch):
        self._capture(monkeypatch)
        f = tmp_path / "a.py"
        f.write_text("# Loop over every row\nfor r in rows:\n    pass\n")
        found = cs.scan(str(f), sweep=llm.sweep())
        assert [(i.line, i.reason) for i in found] == [(1, "narration (model)")]

    def test_a_stated_reason_is_never_sent(self, tmp_path, monkeypatch):
        seen = self._capture(monkeypatch)
        f = tmp_path / "a.py"
        f.write_text(
            "# Skip these because the feed repeats them\nif dup:\n    continue\n"
        )
        assert cs.scan(str(f), sweep=llm.sweep()) == []
        assert seen == []

    def test_a_vetoed_comment_is_never_sent(self, tmp_path, monkeypatch):
        seen = self._capture(monkeypatch)
        f = tmp_path / "a.py"
        f.write_text("# TODO: revisit this\nx = 1\n")
        cs.scan(str(f), sweep=llm.sweep())
        assert seen == []

    def test_already_reported_lines_are_not_sent_twice(self, tmp_path, monkeypatch):
        seen = self._capture(monkeypatch)
        f = tmp_path / "a.py"
        f.write_text("# ===== TOTALS =====\nn = 0\n")
        found = cs.scan(str(f), sweep=llm.sweep())
        assert [i.reason for i in found] == ["banner"]
        assert seen == []

    def test_a_comment_with_no_following_code_is_not_sent(self, tmp_path, monkeypatch):
        seen = self._capture(monkeypatch)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n# Loop over every row\n")
        cs.scan(str(f), sweep=llm.sweep())
        assert seen == []


class TestSweepSkipsInlineComments:
    """A trailing comment annotates the value beside it and is terse by
    necessity; the model reads that terseness as narration. Measured as 4 of 9
    false positives on a real branch diff."""

    def test_inline_comment_is_not_sent(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(
            llm, "classify", lambda pairs, deadline=None: seen.extend(pairs) or set()
        )
        f = tmp_path / "a.py"
        f.write_text("assert called is False  # memory path never engaged\n")
        cs.scan(str(f), sweep=llm.sweep())
        assert seen == []

    def test_own_line_comment_is_still_sent(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(
            llm, "classify", lambda pairs, deadline=None: seen.extend(pairs) or set()
        )
        f = tmp_path / "a.py"
        f.write_text("# Loop over every row\nfor r in rows:\n    pass\n")
        cs.scan(str(f), sweep=llm.sweep())
        assert len(seen) == 1


class TestSweepPreFilterIsSpanAware:
    """The sweep is scoped by `analyze`'s walk, which intersects the diff with
    a block's whole span. Keyed on the opening line alone, editing an interior
    line of a grouped comment would silently drop it from the model batch.
    """

    def test_editing_an_interior_line_still_reaches_the_sweep(
        self, tmp_path, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(
            llm,
            "classify",
            lambda pairs, deadline=None: (
                seen.extend(pairs) or {ln for ln, _, _ in pairs}
            ),
        )
        repo = make_repo(tmp_path)
        f = repo / "a.py"
        f.write_text(
            "# Loop over every row\n# doing the work\nfor r in rows:\n    pass\n"
        )
        commit_all(repo)
        f.write_text(
            "# Loop over every row\n# without exception\nfor r in rows:\n    pass\n"
        )
        assert cs.added_lines(str(f)) == {2}

        found = cs.scan(str(f), sweep=llm.sweep())
        assert [(i.line, i.reason) for i in found] == [(1, "narration (model)")]
        assert seen


class TestSweepDeadline:
    def test_classify_stops_when_the_deadline_passes(self, monkeypatch):
        """A batch cap bounds calls, not latency. Three batches against a
        120s timeout is a six-minute stall on one edit."""
        calls = []

        def slow_urlopen(req, timeout=None):
            calls.append(req)
            time.sleep(0.2)
            raise RuntimeError("counted, not called for real")

        monkeypatch.setattr(urllib.request, "urlopen", slow_urlopen)
        pairs = [(i, f"comment {i}", ["run()"]) for i in range(1, 60)]
        start = time.monotonic()
        llm.classify(pairs, deadline=0.3)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"deadline not honoured: {elapsed:.1f}s"
        assert len(calls) < 5, calls

    def test_classify_without_a_deadline_covers_every_batch(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise RuntimeError("counted")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        pairs = [(i, f"comment {i}", ["run()"]) for i in range(1, 40)]
        llm.classify(pairs)
        assert len(calls) == 4, "39 pairs at LLM_BATCH=12 is 4 batches"

    def test_truncation_is_reported_not_silent(self, monkeypatch, capsys):
        def instant_fail(req, timeout=None):
            raise RuntimeError("unreachable")

        monkeypatch.setattr(urllib.request, "urlopen", instant_fail)
        pairs = [(i, f"comment {i}", ["run()"]) for i in range(1, 40)]
        llm.classify(pairs, deadline=-1)
        err = capsys.readouterr().err
        assert "not sent" in err or "deadline" in err.lower()


class TestTheSweepSeesOnlyWhatTheTiersLeft:
    """The sweep is handed the residue of `analyze`'s own walk, so every
    exemption applies to it: `NEVER`, the language vetoes and the shebang
    skip."""

    @staticmethod
    def residue(src, path):
        seen = []

        def capture(residue, lines):
            seen.extend(b.line for b in residue)
            return []

        cs.analyze(src, path, sweep=capture)
        return seen

    def test_a_language_veto_never_reaches_the_sweep(self):
        assert self.residue("steps:\n  # v6.2.0\n  - uses: a/b@abc\n", "ci.yml") == []

    def test_a_shebang_never_reaches_the_sweep(self):
        assert self.residue("#!/usr/bin/env python3\nx = 1\n", "a.py") == []

    def test_a_fired_comment_never_reaches_the_sweep(self):
        assert self.residue("# Fixed: the cache\nx = 1\n", "a.py") == []

    def test_an_unmatched_comment_does(self):
        src = "# Loop over every row\nfor r in rows:\n    pass\n"
        assert self.residue(src, "a.py") == [1]


class TestVendoredSweepModule:
    @staticmethod
    def run_copy(tmp_path, companion=None):
        vendored = tmp_path / "vendored"
        vendored.mkdir()
        detector = vendored / "comment_slop.py"
        detector.write_bytes(Path(cs.__file__).read_bytes())
        if companion is not None:
            (vendored / "comment_slop_llm.py").write_text(companion)
        source = tmp_path / "a.py"
        source.write_text(
            "# ===== TOTALS =====\nx = 1\n# Loop over every row\nfor r in rows:\n    pass\n"
        )
        return subprocess.run(
            [sys.executable, str(detector), "--all", "--llm", str(source)],
            capture_output=True,
            text=True,
        )

    def test_missing_companion_ignores_another_copy_on_pythonpath(
        self, tmp_path, monkeypatch
    ):
        """Vendoring only the detector must not import a different installation."""
        external = tmp_path / "external"
        external.mkdir()
        (external / "comment_slop_llm.py").write_text(
            'raise RuntimeError("loaded the external sweep")\n'
        )
        monkeypatch.setenv("PYTHONPATH", str(external))
        result = self.run_copy(tmp_path)
        assert result.returncode == 2
        assert "[banner]" in result.stderr
        assert "--llm needs comment_slop_llm.py beside this module" in result.stderr
        assert "loaded the external sweep" not in result.stderr

    def test_compatible_adjacent_companion_is_used(self, tmp_path, monkeypatch):
        external = tmp_path / "external"
        external.mkdir()
        (external / "comment_slop_llm.py").write_text(
            'raise RuntimeError("loaded the external sweep")\n'
        )
        monkeypatch.setenv("PYTHONPATH", str(external))
        result = self.run_copy(
            tmp_path,
            "from comment_slop import Finding\n"
            "LLM_DEADLINE = 20\n"
            "def sweep(deadline=None):\n"
            "    return lambda residue, lines: [Finding(b.line, 'narration (model)', b.body) for b in residue]\n",
        )
        assert result.returncode == 2
        assert "[narration (model)]" in result.stderr
        assert "loaded the external sweep" not in result.stderr

    def test_incompatible_adjacent_companion_keeps_deterministic_findings(
        self, tmp_path
    ):
        result = self.run_copy(tmp_path, "LLM_DEADLINE = 20\n")
        assert result.returncode == 2
        assert "[banner]" in result.stderr
        assert "--llm companion unavailable" in result.stderr

    def test_failing_companion_callback_keeps_deterministic_findings(self, tmp_path):
        result = self.run_copy(
            tmp_path,
            "LLM_DEADLINE = 20\n"
            "def sweep(deadline=None):\n"
            "    def run(residue, lines):\n"
            "        raise RuntimeError('incompatible sweep')\n"
            "    return run\n",
        )
        assert result.returncode == 2
        assert "[banner]" in result.stderr
        assert "--llm companion failed (incompatible sweep)" in result.stderr
