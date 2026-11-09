import os
import sys
from pathlib import Path

import pytest

from flow_engine import agents

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
HERE = Path(__file__).parent
OSS = "gpt-oss-120b-medium"


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FLOW_CLAUDE", str(HERE / "fake_claude.py"))
    monkeypatch.setenv("FLOW_AGY", str(HERE / "fake_agy.py"))
    for k in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOW_BACKOFF_BASE", "0.001")
    monkeypatch.setenv("FLOW_BACKOFF_CAP", "0.01")
    monkeypatch.setenv("FLOW_PIN_BACKOFF_CAP", "0.01")
    monkeypatch.setenv("FAKE_PLAN", "/dev/null")
    monkeypatch.setenv("FAKE_STEPS", str(tmp_path / "steps.json"))
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
