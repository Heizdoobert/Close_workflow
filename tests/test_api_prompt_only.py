"""The api backend serves only prompt-only calls (agents.Opts.prompt_only). A call that may read the repo is refused before any request and falls through to the CLI. Fake server only, no real key."""

import stat
import sys
from pathlib import Path

import pytest

import fake_api
from flow_engine import agents, api, interview

HERE = Path(__file__).parent
KEY = "sk-planted-SECRET-do-not-leak"
ANTHROPIC = "api:anthropic/claude-haiku"
OPENAI = "api:openai/example-model"
ANTHROPIC_MODEL = "anthropic/claude-haiku"
REFUSAL = "invalid argument: api backend only serves prompt-only calls"


@pytest.fixture
def server(monkeypatch):
    srv = fake_api.start()
    monkeypatch.setenv("FLOW_API_BASE_URL", srv.base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    for k in ("FLOW_API_CACHE", "FLOW_BACKENDS", "FLOW_PRICE_OPENAI_EXAMPLE_MODEL", "FLOW_PRICE_ANTHROPIC_CLAUDE_HAIKU"):
        monkeypatch.delenv(k, raising=False)
    api.breaker.clear()
    api.breaker_at.clear()
    api.cache.clear()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    launcher = tmp_path / "fake_claude.sh"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_claude.py"}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FLOW_CLAUDE", str(launcher))
    monkeypatch.setenv("FAKE_PLAN", "/dev/null")
    monkeypatch.setenv("FAKE_STEPS", str(tmp_path / "steps.json"))
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path / "slots"))
    (tmp_path / "steps.json").write_text("{}")
    return tmp_path


def test_api_refuses_a_call_that_is_not_prompt_only_before_any_request(server, tmp_path):
    res = api.run_api(ANTHROPIC_MODEL, "say hi", agents.Opts(str(tmp_path), prompt_only=False))
    assert not res.ok and res.error == REFUSAL and res.transient is False
    assert res.backend == f"api:{ANTHROPIC_MODEL}"
    assert server.last_path is None


def test_default_opts_are_not_prompt_only(server, tmp_path):
    assert agents.Opts(str(tmp_path)).prompt_only is False
    res = api.run_api(ANTHROPIC_MODEL, "say hi", agents.Opts(str(tmp_path)))
    assert not res.ok and res.error == REFUSAL
    assert server.last_path is None


def test_api_serves_a_prompt_only_call(server, tmp_path):
    res = api.run_api(ANTHROPIC_MODEL, "say hi", agents.Opts(str(tmp_path), prompt_only=True))
    assert res.ok and res.text == "hello world"
    assert server.last_path == "/v1/messages"


@pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
def test_refused_api_entry_falls_through_to_the_cli_backend(server, fake_claude, tmp_path):
    trace = []
    res, _, _ = agents.call([OPENAI, "claude:haiku"], "planning agent hello", agents.Opts(str(tmp_path), prompt_only=False), trace)
    assert res.ok and res.backend == "claude:haiku"
    assert server.last_path is None
    assert trace[0]["backend"] == OPENAI and trace[0]["ok"] is False and trace[0]["error"] == REFUSAL
    assert trace[0]["action"] == "fallback to next backend"
    assert trace[-1]["backend"] == "claude:haiku" and trace[-1]["action"] == "done"


def test_prompt_only_api_entry_is_served_without_the_cli(server, fake_claude, tmp_path):
    res, _, _ = agents.call([OPENAI, "claude:haiku"], "planning agent hello", agents.Opts(str(tmp_path), prompt_only=True), [])
    assert res.ok and res.backend == OPENAI and res.text == "hi there"
    assert server.last_path == "/v1/chat/completions"


def test_interview_round_is_sent_as_prompt_only(tmp_path, monkeypatch):
    seen = []

    def stop_after_capture(chain, prompt, o, trace):
        seen.append(o)
        raise RuntimeError("stop: only the options are checked")

    monkeypatch.setattr(agents, "call", stop_after_capture)
    with pytest.raises(RuntimeError):
        interview.step("goal", [], ["claude:haiku"], str(tmp_path))
    assert len(seen) == 1 and seen[0].prompt_only is True
