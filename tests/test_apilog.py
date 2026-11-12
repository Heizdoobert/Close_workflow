"""One JSON log line per API attempt: shape, no key, no prompt, and what run_api writes. No real key; fake server only."""

import json
import subprocess
from types import SimpleNamespace

import pytest

import fake_api
from flow_engine import agents, api, apilog, engine

KEY = "sk-planted-SECRET-do-not-leak"
MODEL = "anthropic/claude-haiku"
USAGE = {"input_tokens": 12, "output_tokens": 5, "cache_read": 4, "cache_create": 3, "thinking": 0}
EXPECTED = {"ts", "event", "role", "backend", "model", "input_tokens", "output_tokens", "cache_read", "cache_create", "cost", "latency_ms", "status"}


@pytest.fixture
def log_file():
    return engine.LOG  # redirected to a temp dir by conftest.isolated_flow_log


@pytest.fixture
def server(monkeypatch, log_file):
    srv = fake_api.start()
    monkeypatch.setenv("FLOW_API_BASE_URL", srv.base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("FLOW_PRICE_ANTHROPIC_CLAUDE_HAIKU", "1.0,0.1,1.25,5.0")
    for k in ("FLOW_API_CACHE", "FLOW_BACKENDS"):
        monkeypatch.delenv(k, raising=False)
    api.breaker.clear()
    api.breaker_at.clear()
    api.cache.clear()
    yield srv
    srv.shutdown()
    srv.server_close()


def _lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_line_is_one_json_object_with_the_expected_keys():
    text = apilog.line("planner", "api:anthropic", MODEL, USAGE, 0.5, 120.4, "ok")
    assert "\n" not in text
    rec = json.loads(text)
    assert set(rec) == EXPECTED
    assert rec["role"] == "planner" and rec["backend"] == "api:anthropic" and rec["model"] == MODEL
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read"], rec["cache_create"]) == (12, 5, 4, 3)
    assert rec["cost"] == 0.5 and rec["latency_ms"] == 120 and rec["status"] == "ok"


def test_unknown_cost_is_null_not_zero():
    rec = json.loads(apilog.line("review", "api:openai", "openai/x", {}, None, 5, "ok"))
    assert rec["cost"] is None


def test_missing_usage_counts_are_zero():
    rec = json.loads(apilog.line("", "api:openai", "openai/x", None, 0.0, 1, "error"))
    assert (rec["input_tokens"], rec["output_tokens"], rec["cache_read"], rec["cache_create"]) == (0, 0, 0, 0)
    assert rec["role"] == ""


def test_line_has_no_key_or_key_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    text = apilog.line("planner", "api:anthropic", MODEL, USAGE, 0.1, 10, "ok")
    assert "sk-" not in text
    assert KEY not in text


def test_run_api_writes_one_line_per_request_and_no_key(server, log_file, tmp_path):
    o = SimpleNamespace(workdir=str(tmp_path), tools=agents.READ_TOOLS, timeout=30, role="planner")
    ok = api.run_api(MODEL, "plan the step", o)
    assert ok.ok
    server.fail_next = 500
    bad = api.run_api(MODEL, "plan the step", o)
    assert not bad.ok and bad.transient
    lines = _lines(log_file)
    assert [x["status"] for x in lines] == ["ok", "transient"]
    assert all(x["event"] == "api" and x["role"] == "planner" and x["backend"] == "api:anthropic" for x in lines)
    assert (lines[0]["input_tokens"], lines[0]["cache_read"], lines[0]["cache_create"], lines[0]["output_tokens"]) == (12, 4, 3, 5)
    assert lines[0]["cost"] == pytest.approx(ok.cost) and lines[0]["cost"] > 0
    assert lines[1]["cost"] == 0.0
    text = log_file.read_text(encoding="utf-8")
    assert KEY not in text
    assert "sk-" not in text
    assert "plan the step" not in text


def test_run_api_logs_unpriced_model_as_null_cost(server, log_file, monkeypatch, tmp_path):
    monkeypatch.delenv("FLOW_PRICE_ANTHROPIC_CLAUDE_HAIKU")
    o = SimpleNamespace(workdir=str(tmp_path), tools=agents.READ_TOOLS, timeout=30, role="")
    res = api.run_api(MODEL, "p", o)
    assert res.ok and res.tokens.get("cost_unknown") == 1
    assert _lines(log_file)[0]["cost"] is None


def test_cache_hit_is_logged_with_zero_cost(server, log_file, monkeypatch, tmp_path):
    env = api.git_env()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base"], cwd=tmp_path, check=True, env=env)
    monkeypatch.setenv("FLOW_API_CACHE", "1")
    o = SimpleNamespace(workdir=str(tmp_path), tools=agents.READ_TOOLS, timeout=30, role="scout")
    first = api.run_api(MODEL, "same prompt", o)
    second = api.run_api(MODEL, "same prompt", o)
    assert first.ok and second.ok and second.tokens == {"cache_hit": 1}
    lines = _lines(log_file)
    assert [x["status"] for x in lines] == ["ok", "cache_hit"]
    assert lines[1]["cost"] == 0.0 and lines[1]["input_tokens"] == 0
    assert KEY not in log_file.read_text(encoding="utf-8")
