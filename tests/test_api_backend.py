"""API backend through the chain (agents.call) and directly (api.run_api), against the fake server. No real key."""

import json
import subprocess
import time

import pytest

import fake_api
from flow_engine import agents, api

KEY = "sk-planted-SECRET-do-not-leak"
ANTHROPIC = "api:anthropic/claude-haiku"
OPENAI = "api:openai/example-model"


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


def chain(spec, workdir, tools=agents.READ_TOOLS, trace=None):
    trace = [] if trace is None else trace
    return agents.call([spec], "say hi", agents.Opts(str(workdir), tools=tools), trace, sleep=lambda s: None)


def git(path, *a):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, env=api.git_env())


def git_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


def test_api_backend_is_registered_next_to_cli_backends():
    assert set(agents.BACKENDS) == {"claude", "agy", "opencode", "api"}


def test_anthropic_success_through_chain_maps_usage_and_headers(server, tmp_path):
    res, spent, tokens = chain(ANTHROPIC, tmp_path)
    assert res.ok and res.text == "hello world"
    assert res.backend == ANTHROPIC
    assert server.last_path == "/v1/messages"
    assert server.last_headers["x-api-key"] == KEY
    assert tokens == {"input_tokens": 12, "cache_read": 4, "cache_create": 3, "output_tokens": 5, "thinking": 0, "cost_unknown": 1}
    assert spent == 0.0


def test_openai_success_has_real_cost_from_price_env(server, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_PRICE_OPENAI_EXAMPLE_MODEL", "1,0.1,1.25,5")
    res, spent, tokens = chain(OPENAI, tmp_path)
    assert res.ok and res.text == "hi there"
    assert server.last_path == "/v1/chat/completions"
    assert server.last_headers["authorization"] == f"Bearer {KEY}"
    # 12 fresh input, 8 cached, 0 cache write, 6 output
    assert res.cost == pytest.approx((12 * 1 + 8 * 0.1 + 6 * 5) / 1_000_000)
    assert spent == pytest.approx(res.cost)
    assert "cost_unknown" not in tokens


def test_unknown_model_gives_cost_unknown_flag_not_silent_zero(server, tmp_path):
    res, spent, tokens = chain(OPENAI, tmp_path)
    assert res.ok and res.cost == 0.0
    assert tokens["cost_unknown"] == 1


def test_missing_key_is_permanent_one_attempt_and_no_request(server, tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    trace = []
    res, _, _ = chain(OPENAI, tmp_path, trace=trace)
    assert not res.ok and res.error == "missing api key for openai" and not res.transient
    assert len(trace) == 1 and trace[0]["action"] == "give up"
    assert server.last_path is None


def test_unknown_provider_is_refused_without_request(server, tmp_path):
    trace = []
    res, _, _ = chain("api:example/test-model", tmp_path, trace=trace)
    assert not res.ok and res.error == "invalid argument: unknown provider example" and not res.transient
    assert len(trace) == 1 and server.last_path is None


def test_write_tools_refused_loudly_never_answered_as_edited(server, tmp_path):
    trace = []
    res, _, _ = chain(ANTHROPIC, tmp_path, tools="Read,Grep,Glob,Edit,Write,Bash", trace=trace)
    assert not res.ok and not res.transient and res.text == ""
    assert res.error == "invalid argument: api backend has no tool loop, write tools are not supported"
    assert len(trace) == 1 and trace[0]["action"] == "give up" and server.last_path is None


def test_breaker_opens_after_five_failures_then_probes_once_after_cool_down(server, tmp_path):
    o = agents.Opts(str(tmp_path))
    for _ in range(api.BREAKER_LIMIT):
        server.fail_next = 500
        res = api.run_api("anthropic/claude-haiku", "say hi", o)
        assert not res.ok and res.transient and res.error == "HTTP 500 Internal Server Error"
    server.fail_next = 500
    res = api.run_api("anthropic/claude-haiku", "say hi", o)
    assert not res.ok and res.transient and res.error == "breaker open"
    assert server.fail_next == 500  # the open breaker sent no request
    server.fail_next = None
    api.breaker_at["anthropic"] = time.time() - api.BREAKER_COOL - 1  # cool-down over: the next request is the probe
    res = api.run_api("anthropic/claude-haiku", "say hi", o)
    assert res.ok and res.text == "hello world"
    assert "anthropic" not in api.breaker  # a success resets the count


def test_breaker_is_per_provider(server, tmp_path):
    o = agents.Opts(str(tmp_path))
    api.breaker["anthropic"] = api.BREAKER_LIMIT
    api.breaker_at["anthropic"] = time.time()
    assert api.run_api("openai/example-model", "say hi", o).ok


def test_cache_hit_costs_nothing_and_is_marked(server, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_API_CACHE", "1")
    repo = git_repo(tmp_path / "wt")
    first, _, _ = chain(ANTHROPIC, repo)
    assert first.ok and first.text == "hello world"
    server.fail_next = 500  # a second request would fail: only a cache hit can succeed
    trace = []
    res, spent, tokens = chain(ANTHROPIC, repo, trace=trace)
    assert res.ok and res.text == "hello world" and res.error == ""
    assert res.cost == 0.0 and spent == 0.0
    assert tokens == {"cache_hit": 1}
    assert server.fail_next == 500
    assert trace[-1]["action"] == "done"


def test_cache_is_off_by_default(server, tmp_path):
    repo = git_repo(tmp_path / "wt")
    assert chain(ANTHROPIC, repo)[0].ok
    server.fail_next = 500
    res = api.run_api("anthropic/claude-haiku", "say hi", agents.Opts(str(repo)))
    assert not res.ok and res.error == "HTTP 500 Internal Server Error"


def test_cache_key_changes_with_head_and_prompt(server, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_API_CACHE", "1")
    repo = git_repo(tmp_path / "wt")
    assert chain(ANTHROPIC, repo)[0].ok
    git(repo, "commit", "-q", "--allow-empty", "-m", "next")
    server.fail_next = 500
    res, _, _ = chain(ANTHROPIC, repo)  # new HEAD: no hit, the request goes out and fails once, then the retry succeeds
    assert res.ok and res.text == "hello world" and "cache_hit" not in res.tokens


def test_no_cache_outside_git(server, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_API_CACHE", "1")
    assert chain(ANTHROPIC, tmp_path)[0].ok
    assert api.cache == {}


def test_planted_key_appears_in_no_result_or_trace(server, tmp_path):
    server.fail_next = 401
    server.fail_body = {"error": f"bad key {KEY}"}
    trace = []
    res, _, _ = chain(ANTHROPIC, tmp_path, trace=trace)
    assert not res.ok
    assert KEY not in json.dumps([res.error, res.text, res.backend, res.tokens, trace])


def test_planted_key_absent_when_transport_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("FLOW_API_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    trace = []
    res, _, _ = chain(ANTHROPIC, tmp_path, trace=trace)
    assert not res.ok and res.error and res.transient
    assert KEY not in json.dumps([res.error, trace])
