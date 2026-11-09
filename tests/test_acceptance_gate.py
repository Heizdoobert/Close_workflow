import os
import shutil
import subprocess

import pytest

from flow_engine import gate, plan

TWO_ASSERTS = "def test_x():\n    assert 1 == 1\n    assert 2 == 2\n"


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "tests" / "test_x.py").write_text(TWO_ASSERTS)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def step(acc):
    return {"steps": [{"id": "s1", "title": "t", "reason": "r", "deps": [], "files": [], "acceptance": acc, "prompt": "p"}]}


@pytest.mark.parametrize("acc", ["", "   ", "true", ":", "exit 0", "echo done", "printf ok", "true && echo ok", "echo a | true"])
def test_validate_rejects_trivial_acceptance_and_names_the_step(acc):
    with pytest.raises(plan.PlanError, match="step s1"):
        plan.validate(step(acc))


@pytest.mark.parametrize("acc", ["python3 -m pytest -q tests/test_x.py", "echo start && python3 -m pytest -q tests/test_x.py", "test -f a.py"])
def test_validate_accepts_real_acceptance(acc):
    assert plan.validate(step(acc))["steps"][0]["acceptance"] == acc


def test_constraints_passes_on_an_untouched_repo(repo):
    res = gate.constraints(repo)
    assert res["ok"] and res["assert_net"] == 0 and res["files"] == []


def test_constraints_fails_after_removing_one_assert(repo):
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert 1 == 1\n")
    res = gate.constraints(repo)
    assert not res["ok"] and res["assert_net"] == -1
    assert res["files"] == ["tests/test_x.py"]


def test_constraints_fails_after_deleting_the_test_file(repo):
    os.remove(repo / "tests" / "test_x.py")
    res = gate.constraints(repo)
    assert not res["ok"] and res["deleted_tests"] == ["tests/test_x.py"]
    assert res["files"] == ["tests/test_x.py"]


def test_constraints_fails_after_adding_a_skip_marker(repo):
    skip = "@pytest.mark." + "skip"  # split so this file's own diff does not trip the constraints gate
    (repo / "tests" / "test_x.py").write_text("import pytest\n\n" + skip + "\n" + TWO_ASSERTS)
    res = gate.constraints(repo)
    assert not res["ok"] and res["weakened"] == 1 and res["assert_net"] == 0
    assert res["files"] == ["tests/test_x.py"]


def test_constraints_passes_a_refactor_that_keeps_the_assert_count(repo):
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    value = 1 + 1\n    assert value == 2\n    assert 1 == 1\n")
    res = gate.constraints(repo)
    assert res["ok"] and res["assert_net"] == 0 and res["removed_tests"] == 0


def test_run_gates_reports_the_constraints_gate(repo, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(gate.shutil, "which", lambda name: None if name == "gitleaks" else real_which(name))
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert 1 == 1\n")
    res = gate.run_gates(str(repo))
    assert not res["ok"] and not res["gates"]["constraints"]["ok"]
    assert res["gates"]["constraints"]["assert_net"] == -1
