"""The plan acceptance command runs agent code: sandboxed without claude's credentials or API keys, with an explicit timeout."""

import subprocess

from flow_engine import plan, sandbox


class Recorder:
    """Stands in for subprocess.Popen: records how accept() started the command and what it waited for."""

    returncode = 0
    last = None

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        Recorder.last = self

    def wait(self, timeout=None):
        return 0


class TimingOutRecorder(Recorder):
    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(self.argv, timeout)


def _step(cmd="python3 -m pytest -q"):
    return {"id": "s1", "acceptance": cmd}


def test_accept_env_drops_secrets_and_keeps_path(monkeypatch, tmp_path):
    monkeypatch.setattr(plan.subprocess, "Popen", Recorder)
    monkeypatch.setattr(sandbox, "available", lambda: None)
    monkeypatch.setenv("REDIS_PASSWORD", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-value-1234567890")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    res = plan.accept(_step(), str(tmp_path))

    env = Recorder.last.kwargs["env"]
    assert "REDIS_PASSWORD" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert res["ok"] is True


def test_accept_is_wrapped_in_bwrap_without_claude_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(plan.subprocess, "Popen", Recorder)
    monkeypatch.setattr(sandbox, "available", lambda: "bwrap")
    monkeypatch.delenv("FLOW_GATE_SANDBOX", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()

    plan.accept(_step(), str(wt))

    argv = Recorder.last.argv
    assert argv[0] == "bwrap"
    for i, arg in enumerate(argv[:-1]):
        if arg == "--bind":
            assert argv[i + 1] not in (str(home / ".claude"), str(home / ".claude.json"))
    assert "--tmpfs" in argv
    assert str(home / ".claude") in argv


def test_flow_gate_sandbox_0_runs_unwrapped(monkeypatch, tmp_path):
    monkeypatch.setattr(plan.subprocess, "Popen", Recorder)
    monkeypatch.setattr(sandbox, "available", lambda: "bwrap")
    monkeypatch.setenv("FLOW_GATE_SANDBOX", "0")

    plan.accept(_step(), str(tmp_path))

    assert Recorder.last.argv[0] == "bash"
    assert "bwrap" not in Recorder.last.argv


def test_accept_timeout_is_explicit(monkeypatch, tmp_path):
    monkeypatch.setattr(plan.subprocess, "Popen", TimingOutRecorder)
    monkeypatch.setattr(plan, "_kill_group", lambda p: None)
    monkeypatch.setattr(sandbox, "available", lambda: None)
    monkeypatch.setenv("FLOW_ACCEPT_TIMEOUT", "7")

    res = plan.accept(_step(), str(tmp_path))

    assert res["timeout"] is True
    assert res["rc"] is None
    assert res["ok"] is False
    assert res["tail"] == "timeout after 7 s"
