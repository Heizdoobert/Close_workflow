import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def tracked(*patterns):
    r = subprocess.run(["git", "ls-files", "-s", "--", *patterns], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        pytest.skip("not a git checkout")
    return [l.split(None, 3) for l in r.stdout.splitlines()]


def test_shell_entry_points_are_executable_in_git():
    rows = [r for r in tracked("*.sh") if "/" not in r[3]]  # the entry points at the root; legacy/ holds archived scripts nobody runs
    assert rows and [r[3] for r in rows if r[0] != "100755"] == []  # a fresh clone must run ./install.sh (the mount here hides modes: core.fileMode=false)


@pytest.mark.skipif(sys.platform == "win32", reason="run_task.sh is a bash shim")
def test_shim_runs_its_own_engine_when_cwd_holds_another(tmp_path):
    fake = tmp_path / "flow_engine"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "__main__.py").write_text("print('WRONG ENGINE')\n")
    r = subprocess.run([str(ROOT / "run_task.sh"), "where", "no-such-plan"], cwd=tmp_path, capture_output=True, text=True)
    assert "WRONG ENGINE" not in r.stdout  # the caller's cwd must not shadow the installed engine
