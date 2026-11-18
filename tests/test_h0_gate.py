"""H0 step s3: the pytest gate runs without secrets, under the sandbox when bwrap is there, and reports a timeout explicitly."""

import subprocess
import sys

import pytest

from flow_engine import gate, sandbox


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "tests/test_x.py"], check=True)  # ls-files lists only tracked files, so the gate needs it staged
    subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    return str(repo)


@pytest.fixture
def calls(monkeypatch):
    """Record every gate._run call; the pytest call gets a canned pass (or whatever a test sets) so no real suite runs."""
    real_run = gate._run
    recorded = []
    state = {"pytest_result": None}

    def fake_run(root, cmd, **kw):
        recorded.append((list(cmd), kw))
        if "pytest" in cmd:
            return state["pytest_result"] or subprocess.CompletedProcess(cmd, 0, "1 passed\n", "")
        return real_run(root, cmd, **kw)

    monkeypatch.setattr(gate, "_run", fake_run)
    monkeypatch.setattr(sandbox, "available", lambda: None)
    monkeypatch.delenv("FLOW_GATE_SANDBOX", raising=False)
    monkeypatch.delenv("FLOW_GATE_TESTS_TIMEOUT", raising=False)
    return recorded, state


def _pytest_call(recorded):
    return next(c for c in recorded if "pytest" in c[0])


def test_pytest_gate_env_drops_secrets_and_keeps_path(tmp_path, monkeypatch, calls):
    recorded, _state = calls
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    res = gate._run_gates(_repo(tmp_path), None)
    argv, kw = _pytest_call(recorded)
    assert res["gates"]["tests"]["ok"] is True
    assert "REDIS_PASSWORD" not in kw["env"]
    assert "ANTHROPIC_API_KEY" not in kw["env"]
    assert "PATH" in kw["env"]


def test_pytest_gate_under_bwrap_hides_claude_config(tmp_path, monkeypatch, calls):
    recorded, _state = calls
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox, "available", lambda: "bwrap")
    monkeypatch.setattr(gate.shutil, "which", lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else None)
    gate._run_gates(_repo(tmp_path), None)
    argv, _kw = _pytest_call(recorded)
    assert argv[0] == "bwrap"
    for path in (str(home / ".claude"), str(home / ".claude.json")):
        assert not any(argv[i] == "--bind" and argv[i + 1] == path for i in range(len(argv) - 1))


def test_pytest_gate_with_sandbox_off_runs_unwrapped(tmp_path, monkeypatch, calls):
    recorded, _state = calls
    monkeypatch.setenv("FLOW_GATE_SANDBOX", "0")
    monkeypatch.setattr(sandbox, "available", lambda: "bwrap")
    monkeypatch.setattr(gate.shutil, "which", lambda name, *a, **k: "/usr/bin/bwrap" if name == "bwrap" else None)
    gate._run_gates(_repo(tmp_path), None)
    argv, _kw = _pytest_call(recorded)
    assert argv[0] != "bwrap"
    assert argv[:3] == [sys.executable, "-m", "pytest"]


def test_pytest_gate_timeout_is_reported(tmp_path, calls):
    recorded, state = calls
    state["pytest_result"] = subprocess.CompletedProcess([], 124, "", "timeout after 1200 s")
    res = gate._run_gates(_repo(tmp_path), None)
    tests = res["gates"]["tests"]
    assert tests["timeout"] is True
    assert tests["ok"] is False
    assert "timeout after 1200 s" in tests["findings"][0]
    _argv, kw = _pytest_call(recorded)
    assert kw["timeout"] == 1200
