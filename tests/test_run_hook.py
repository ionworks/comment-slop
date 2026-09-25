"""`run_hook.sh`, the PostToolUse wrapper: exit 2 for findings and nothing else."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

PKG = Path(__file__).resolve().parent.parent
WRAPPER = PKG / "run_hook.sh"


class TestTheHookWrapperOnlyExitsTwoForFindings:
    """Exit 2 means "block and show this to the agent". Every interpreter also
    exits 2 for its own failures, so the code is re-derived from the output."""

    def run(self, path, env=None, wrapper=WRAPPER):
        full = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        full.update(env or {})
        r = subprocess.run(
            ["sh", str(wrapper)],
            input=json.dumps({"tool_input": {"file_path": str(path)}}),
            capture_output=True,
            text=True,
            env=full,
            timeout=120,
        )
        return r.returncode, r.stderr

    @pytest.fixture
    def clean(self, tmp_path):
        p = tmp_path / "clean.py"
        p.write_text("x = 1\n", encoding="utf-8")
        return p

    @pytest.fixture
    def dirty(self, tmp_path):
        p = tmp_path / "dirty.py"
        p.write_text("# Fixed: use the cache\nx = 1\n", encoding="utf-8")
        return p

    @pytest.fixture
    def fake_root(self, tmp_path):
        """A project dir with a version file of the test's choosing. The project
        dir is where the pin is read from, and nothing else."""

        def make(version=None):
            root = tmp_path / "proj"
            root.mkdir()
            if version is not None:
                (root / ".python-version").write_text(version, encoding="utf-8")
            return {"CLAUDE_PROJECT_DIR": str(root)}

        return make

    def test_findings_exit_two(self, dirty):
        code, err = self.run(dirty)
        assert code == 2 and err.startswith("comment-slop: ")

    def test_a_clean_file_exits_zero(self, clean):
        assert self.run(clean)[0] == 0

    def test_a_broken_uv_cache_does_not_report_findings(self, clean, tmp_path):
        bad = tmp_path / "nocache"
        bad.mkdir(mode=0o000)
        try:
            code, _ = self.run(clean, {"UV_CACHE_DIR": str(bad)})
        finally:
            bad.chmod(0o755)
        assert code == 0

    def test_a_broken_uv_still_reports_real_findings(self, dirty, tmp_path):
        """The fallback matters: a broken uv must degrade to python3, not to
        silence."""
        bad = tmp_path / "nocache2"
        bad.mkdir(mode=0o000)
        try:
            code, err = self.run(dirty, {"UV_CACHE_DIR": str(bad)})
        finally:
            bad.chmod(0o755)
        assert code == 2 and "edit-narration" in err

    def test_the_pinned_version_is_read_from_the_project_dir(
        self, dirty, fake_root, tmp_path
    ):
        """uv finds `.python-version` by walking up from its cwd, which the hook
        does not control -- from outside the repo it picks a different
        interpreter. So the wrapper reads the project dir's version and passes
        it. A version uv cannot satisfy is what makes that visible: the run has
        to reach the fallback, and the stub announces itself there. Unpinned,
        uv would succeed and report findings instead."""
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "python3").write_text(
            "#!/bin/sh\nprintf 'fallback ran\\n'\n", encoding="utf-8"
        )
        (stub / "python3").chmod(0o755)
        env = fake_root("9.9\n")
        env["PATH"] = f"{stub}:{os.environ['PATH']}"
        code, err = self.run(dirty, env)
        assert code == 0 and "fallback ran" in err

    def test_the_pin_never_triggers_an_interpreter_download(self, dirty, tmp_path):
        """`uv run --python=X` satisfies an unmet pin by *downloading* X, so a
        post-edit hook would block on a network fetch to run a stdlib-only
        script. --no-python-downloads is what makes the comment above the pin
        true: unsatisfiable has to fail, so the fallback can run. The real
        version is satisfiable on any dev machine, so a stub uv records the argv
        instead — and exits nonzero, standing in for the pin it cannot meet."""
        stub = tmp_path / "bin"
        stub.mkdir()
        argv = tmp_path / "argv.txt"
        (stub / "uv").write_text(
            f'#!/bin/sh\nprintf \'%s\\n\' "$@" >"{argv}"\nexit 1\n', encoding="utf-8"
        )
        (stub / "uv").chmod(0o755)
        code, err = self.run(dirty, {"PATH": f"{stub}:{os.environ['PATH']}"})
        assert "--no-python-downloads" in argv.read_text().split("\n")
        # And the run still degrades to python3 rather than to silence.
        assert code == 2 and "edit-narration" in err

    def test_a_broken_interpreter_choice_does_not_report_findings(
        self, clean, fake_root
    ):
        env = fake_root()
        # No version file, so UV_PYTHON is again what chooses the interpreter.
        env["UV_PYTHON"] = "python9.9"
        assert self.run(clean, env)[0] == 0

    def test_no_uv_on_path_still_works(self, dirty):
        code, err = self.run(dirty, {"PATH": "/usr/bin:/bin"})
        assert code == 2 and "edit-narration" in err

    def test_a_malformed_payload_exits_zero(self):
        r = subprocess.run(
            ["sh", str(WRAPPER)],
            input="not json",
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert r.returncode == 0

    def test_a_copy_outside_any_repo_finds_its_detector(self, dirty, tmp_path):
        """The detector is found beside the wrapper, not under
        `$CLAUDE_PROJECT_DIR`. A project-relative path fixes one repo's layout:
        anywhere else python3 cannot open the script and exits 2 for itself,
        the wrapper -- correctly -- reads that as no findings, and every edit
        passes unchecked."""
        copy = tmp_path / "vendored"
        (copy / "src").mkdir(parents=True)
        shutil.copy(WRAPPER, copy / "run_hook.sh")
        shutil.copy(PKG / "src" / "comment_slop.py", copy / "src")
        env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
        code, err = self.run(dirty, env, copy / "run_hook.sh")
        assert code == 2 and "edit-narration" in err

    @pytest.mark.parametrize("form", ["absolute", "relative", "chained"])
    def test_a_symlinked_registration_finds_its_detector(self, dirty, tmp_path, form):
        """`$0` names the link, and `src/` sits beside the file it points at.
        Resolved from the link's own directory, the script is not there and
        every edit passes unchecked."""
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        link = hooks / "comment-slop.sh"
        if form == "absolute":
            link.symlink_to(WRAPPER)
        elif form == "relative":
            link.symlink_to(os.path.relpath(WRAPPER, hooks))
        else:
            middle = tmp_path / "middle.sh"
            middle.symlink_to(WRAPPER)
            link.symlink_to(os.path.relpath(middle, hooks))
        code, err = self.run(dirty, wrapper=link)
        assert code == 2 and "edit-narration" in err, err

    def test_a_missing_detector_is_an_error_not_a_pass(self, dirty, tmp_path):
        """Exit 0 would read as a clean edit when nothing was checked."""
        copy = tmp_path / "wrapper-only"
        copy.mkdir()
        shutil.copy(WRAPPER, copy / "run_hook.sh")
        code, err = self.run(dirty, wrapper=copy / "run_hook.sh")
        assert code == 1 and "no detector at" in err

    @pytest.fixture
    def stub_path(self, tmp_path):
        """PATH with stub `uv`/`python3` scripts ahead of the real ones."""

        def make(**scripts):
            stub = tmp_path / "stubs"
            stub.mkdir()
            for name, body in scripts.items():
                (stub / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
                (stub / name).chmod(0o755)
            return {"PATH": f"{stub}:{os.environ['PATH']}"}

        return make

    def test_the_prefix_on_a_clean_exit_is_not_findings(self, clean, stub_path):
        """`warn` prints `comment-slop:` too, and a run that only warned exits
        0. Only exit 2 with the prefix blocks the edit."""
        env = stub_path(uv="echo 'comment-slop: local model unreachable'; exit 0")
        code, err = self.run(clean, env)
        assert code == 0 and "local model unreachable" in err

    def test_the_fallbacks_own_exit_code_decides(self, clean, stub_path):
        """The fallback's status replaces uv's, or a failed uv's 2 would stand
        behind whatever python3 printed."""
        env = stub_path(
            uv="exit 2",
            python3="echo 'comment-slop: local model unreachable'; exit 0",
        )
        code, err = self.run(clean, env)
        assert code == 0 and "local model unreachable" in err

    def test_a_syntax_warning_does_not_hide_findings(self, tmp_path):
        """`ast.parse` warns on an invalid escape. Printed ahead of the report,
        the warning would make the output open with something other than
        `comment-slop:`, so the wrapper would exit 0 and the agent see nothing."""
        p = tmp_path / "warns.py"
        p.write_text('P = "\\d+"\n# Fixed: use the cache\nx = 1\n', encoding="utf-8")
        code, err = self.run(p)
        assert code == 2 and err.startswith("comment-slop: "), err
