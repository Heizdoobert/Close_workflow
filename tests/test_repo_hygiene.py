import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def tracked(*patterns):
    r = subprocess.run(["git", "ls-files", "-s", "--", *patterns], cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        pytest.skip("not a git checkout")
    return [l.split(None, 3) for l in r.stdout.splitlines()]


def test_shell_entry_points_are_executable_in_git():
    rows = tracked("*.sh")
    assert rows and [r[3] for r in rows if r[0] != "100755"] == []  # a fresh clone must run ./install.sh (the mount here hides modes: core.fileMode=false)
