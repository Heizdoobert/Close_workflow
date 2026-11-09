import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flow_engine import agents

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
HERE = Path(__file__).parent


def launcher(d, script):
    import stat
    w = Path(d) / (Path(script).stem + ".sh")
    w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    return str(w)
OSS = "gpt-oss-120b-medium"


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FLOW_CLAUDE", launcher(tmp_path, str(HERE / "fake_claude.py")))
    monkeypatch.setenv("FLOW_AGY", launcher(tmp_path, str(HERE / "fake_agy.py")))
    for k in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOW_BACKOFF_BASE", "0.001")
    monkeypatch.setenv("FLOW_BACKOFF_CAP", "0.01")
    monkeypatch.setenv("FLOW_PIN_BACKOFF_CAP", "0.01")
    monkeypatch.setenv("FAKE_PLAN", "/dev/null")
    monkeypatch.setenv("FAKE_STEPS", str(tmp_path / "steps.json"))
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path / "slots"))
    (tmp_path / "steps.json").write_text("{}")
    return tmp_path


def calls(d):
    p = d / "calls.log"
    return [l.split("\t") for l in p.read_text().splitlines()] if p.exists() else []


def run(chain, d):
    # "planning agent" makes the fake read FAKE_PLAN; /dev/null gives an empty but successful answer
    return agents.call(chain, "planning agent hello", agents.Opts(str(d)), [])


def test_transient_and_permanent_patterns():
    for t in ("API Error: 503 Service Unavailable", "model overloaded", "rate limit hit", "ECONNRESET", "timeout after 60s"):
        assert agents.TRANSIENT.search(t), t
    for t in ("invalid request", "401 invalid api key"):
        assert not agents.TRANSIENT.search(t), t
    assert agents.PERMANENT.search("401 invalid api key") and not agents.PERMANENT.search("503 overloaded")
    for env_err in ("OCI runtime exec failed: chdir to cwd failed: no such file or directory", "docker daemon not running"):
        assert agents.PERMANENT.search(env_err)  # environment errors: retrying for minutes cannot help


def test_503_is_retried_with_backoff_then_succeeds(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_503_FIRST", "2")
    trace = []
    res, cost, _ = agents.call(["claude:haiku"], "planning agent", agents.Opts(str(fakes)), trace)
    assert res.ok and cost > 0
    assert [e["action"].split(" in")[0] for e in trace] == ["retry", "retry", "done"]
    assert len(calls(fakes)) == 3


def test_hard_error_falls_back_to_next_backend_without_retrying_it(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_BAD_MODEL", "haiku")
    res, _, _ = run(["claude:haiku", "claude:sonnet"], fakes)
    assert res.ok and res.backend == "claude:sonnet"
    assert [c[1] for c in calls(fakes) if c[0] == "claude"] == ["haiku", "sonnet"]


def test_policy_chains():
    assert agents.policy_chain(OSS) == [f"agy:{OSS}!"]
    assert agents.policy_chain("GPT-OSS 120B (Medium)") == ["agy:GPT-OSS 120B (Medium)!"]
    assert agents.policy_chain("gemini-3.8-flash-high") == [f"agy:{OSS}+", "agy:gemini-3.8-flash-high", "agy:gemini-3.8-flash-low"]
    assert agents.policy_chain("claude-sonnet-4-6") == [] and agents.policy_chain(None) == []


def test_explicit_backends_env_beats_policy(fakes, monkeypatch):
    monkeypatch.setenv("FLOW_BACKENDS", "claude:sonnet")
    assert agents.chain_from_env("claude:haiku", OSS) == ["claude:sonnet"]


def test_gpt_oss_inherited_and_pinned_through_503_and_odd_errors(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_503_FIRST", "4")
    monkeypatch.setenv("FAKE_AGY_FLAKY_FIRST", "2")
    chain = agents.chain_from_env("claude:haiku", OSS)
    trace = []
    res, _, _ = agents.call(chain, "planning agent", agents.Opts(str(fakes)), trace)
    assert res.ok and res.backend == f"agy:{OSS}"
    agy = [c for c in calls(fakes) if c[0] == "agy"]
    assert len(agy) >= 7 and {c[1] for c in agy} == {OSS}      # same model on every single attempt
    assert not [c for c in calls(fakes) if c[0] == "claude"]    # nothing else was ever called
    assert all(c[2] == "conv-1" for c in agy[1:])               # retries continued the same conversation


def test_pinned_permanent_error_fails_loudly_instead_of_switching_model(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_DEAD", OSS)
    trace = []
    res, _, _ = agents.call([f"agy:{OSS}!", "claude:haiku", "agy:gemini-3.8-flash-low"], "planning agent", agents.Opts(str(fakes)), trace)
    assert not res.ok and "401" in res.error
    assert [c[1] for c in calls(fakes)] == [OSS]                # one attempt: retrying a dead key is pointless; no other model
    assert "no fallback" in trace[-1]["action"]


def test_pinned_retries_exhausted_still_no_fallback(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_FLAKY_FIRST", "100")
    monkeypatch.setenv("FLOW_PIN_RETRIES", "3")
    res, _, _ = agents.call([f"agy:{OSS}!", "claude:haiku"], "planning agent", agents.Opts(str(fakes)), [])
    assert not res.ok
    assert [c[1] for c in calls(fakes)] == [OSS] * 4


def test_gemini_flash_parent_prefers_gpt_oss_and_stays_on_it_through_503(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_503_FIRST", "3")
    chain = agents.chain_from_env("claude:haiku", "gemini-3.8-flash-high")
    res, _, _ = agents.call(chain, "planning agent", agents.Opts(str(fakes)), [])
    assert res.ok and {c[1] for c in calls(fakes)} == {OSS}


def test_gemini_flash_parent_goes_lighter_only_when_gpt_oss_truly_unusable(fakes, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_DEAD", OSS)
    chain = agents.chain_from_env("claude:haiku", "gemini-3.8-flash-high")
    res, _, _ = agents.call(chain, "planning agent", agents.Opts(str(fakes)), [])
    assert res.ok and res.backend == "agy:gemini-3.8-flash-high"
    assert [c[1] for c in calls(fakes)] == [OSS, "gemini-3.8-flash-high"]
    monkeypatch.setenv("FAKE_AGY_DEAD", f"{OSS},gemini-3.8-flash-high")
    res, _, _ = agents.call(chain, "planning agent", agents.Opts(str(fakes)), [])
    assert res.ok and res.backend == "agy:gemini-3.8-flash-low"


def fake_claude_run(on_call):
    """Stand-in for subprocess.run: a successful claude JSON answer; on_call(cmd) may raise or sleep."""
    def run(cmd, **kw):
        on_call(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"type": "result", "result": "ok", "is_error": False, "total_cost_usd": 0.0, "usage": {}}), stderr="")
    return run


def test_global_agent_cap_bounds_concurrent_subprocesses(fakes, monkeypatch):
    monkeypatch.setenv("FLOW_MAX_AGENTS", "2")
    monkeypatch.setenv("FLOW_CLAUDE", "claude-fake")  # never executed: subprocess.run is replaced below
    lock, state = threading.Lock(), {"active": 0, "peak": 0}

    def enter(cmd):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1

    monkeypatch.setattr(agents.subprocess, "run", fake_claude_run(enter))
    results = []
    threads = [threading.Thread(target=lambda: results.append(agents.call(["claude:haiku"], "p", agents.Opts(str(fakes)), []))) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(results) == 5 and all(r[0].ok for r in results)
    assert state["peak"] == 2  # capped at FLOW_MAX_AGENTS, and the cap was actually reached


def test_slot_released_after_timeout_so_later_calls_still_run(fakes, monkeypatch):
    monkeypatch.setenv("FLOW_MAX_AGENTS", "1")
    monkeypatch.setenv("FLOW_CLAUDE", "claude-fake")

    def hang(cmd):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(agents.subprocess, "run", fake_claude_run(hang))
    with pytest.raises(subprocess.TimeoutExpired):
        agents._run(["claude-fake"], agents.Opts(str(fakes)))
    monkeypatch.setattr(agents.subprocess, "run", fake_claude_run(lambda cmd: None))
    done = []
    t = threading.Thread(target=lambda: done.append(agents._run(["claude-fake"], agents.Opts(str(fakes)))), daemon=True)
    t.start()
    t.join(10)
    assert done and done[0].returncode == 0  # a leaked slot would hang here instead


def test_slot_released_after_oserror(fakes, monkeypatch):
    monkeypatch.setenv("FLOW_MAX_AGENTS", "1")

    def missing(cmd, **kw):
        raise OSError("exec failed")

    monkeypatch.setattr(agents.subprocess, "run", missing)
    with pytest.raises(OSError):
        agents._run(["nope"], agents.Opts(str(fakes)))
    monkeypatch.setattr(agents.subprocess, "run", fake_claude_run(lambda cmd: None))
    assert agents._run(["ok"], agents.Opts(str(fakes))).returncode == 0


def test_file_slot_shared_with_other_processes(fakes, monkeypatch):
    import fcntl
    monkeypatch.setenv("FLOW_MAX_AGENTS", "1")
    slots = Path(os.environ["FLOW_SLOT_DIR"])
    slots.mkdir(parents=True, exist_ok=True)
    held = open(slots / "slot-0.lock", "a+")  # stands in for another plan runner process holding the only slot
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(agents.subprocess, "run", fake_claude_run(lambda cmd: None))
    done = []
    t = threading.Thread(target=lambda: done.append(agents._run(["x"], agents.Opts(str(fakes)))), daemon=True)
    t.start()
    t.join(0.3)
    assert not done  # blocked while the other process holds the slot
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    t.join(10)
    assert done and done[0].returncode == 0


def test_max_agents_env_parsing(monkeypatch):
    monkeypatch.delenv("FLOW_MAX_AGENTS", raising=False)
    assert agents.max_agents() == 4
    for v, n in (("0", 1), ("-3", 1), ("7", 7), ("many", 4)):
        monkeypatch.setenv("FLOW_MAX_AGENTS", v)
        assert agents.max_agents() == n, v
