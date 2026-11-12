"""API retry and breaker rules: Retry-After is honoured by agents.call, and only transient failures open the breaker. Fake server only, no real key."""

import time

import pytest

import fake_api
from flow_engine import agents, api

KEY = "sk-planted-SECRET-do-not-leak"
ANTHROPIC = "api:anthropic/claude-haiku"
ANTHROPIC_MODEL = "anthropic/claude-haiku"


@pytest.fixture(autouse=True)
def clean_breaker_and_cache():
    api.breaker.clear()
    api.breaker_at.clear()
    api.cache.clear()
    yield
    api.breaker.clear()
    api.breaker_at.clear()
    api.cache.clear()


@pytest.fixture
def server(monkeypatch):
    srv = fake_api.start()
    monkeypatch.setenv("FLOW_API_BASE_URL", srv.base)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("FLOW_BACKOFF_CAP", "60")
    monkeypatch.setenv("FLOW_BACKOFF_BASE", "0.001")
    for k in ("FLOW_API_CACHE", "FLOW_BACKENDS", "FLOW_PRICE_ANTHROPIC_CLAUDE_HAIKU"):
        monkeypatch.delenv(k, raising=False)
    yield srv
    srv.shutdown()
    srv.server_close()


def call_with_sleep(spec, workdir, sleeps):
    return agents.call([spec], "say hi", agents.Opts(str(workdir), prompt_only=True), [], sleep=sleeps.append)


def test_429_retry_after_is_the_delay_then_success(server, tmp_path):
    server.fail_next = 429
    server.retry_after = "2"
    sleeps = []
    res, _, _ = call_with_sleep(ANTHROPIC, tmp_path, sleeps)
    assert sleeps == [2.0]
    assert res.ok and res.text == "hello world"


def test_503_retry_after_is_the_delay_then_success(server, tmp_path):
    server.fail_next = 503
    server.retry_after = "2"
    sleeps = []
    res, _, _ = call_with_sleep(ANTHROPIC, tmp_path, sleeps)
    assert sleeps == [2.0]
    assert res.ok and res.text == "hello world"


def test_retry_after_longer_than_the_ceiling_is_capped(server, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_BACKOFF_CAP", "1")
    server.fail_next = 429
    server.retry_after = "5"
    sleeps = []
    res, _, _ = call_with_sleep(ANTHROPIC, tmp_path, sleeps)
    assert sleeps == [1.0]
    assert res.ok


def test_five_400_answers_do_not_open_the_breaker(server, tmp_path):
    o = agents.Opts(str(tmp_path), prompt_only=True)
    for _ in range(api.BREAKER_LIMIT):
        server.fail_next = 400
        res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
        assert not res.ok and not res.transient and res.error == "HTTP 400 Bad Request"
    assert api.breaker.get("anthropic", 0) == 0
    res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
    assert res.ok and res.text == "hello world"


def test_five_503_answers_open_the_breaker(server, tmp_path):
    o = agents.Opts(str(tmp_path), prompt_only=True)
    for _ in range(api.BREAKER_LIMIT):
        server.fail_next = 503
        res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
        assert not res.ok and res.transient
    server.fail_next = 503
    res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
    assert not res.ok and res.error == "breaker open" and res.transient
    assert server.fail_next == 503  # the open breaker sent no request


def test_failed_half_open_probe_reopens_for_a_full_cool_down(server, tmp_path):
    o = agents.Opts(str(tmp_path), prompt_only=True)
    api.breaker["anthropic"] = api.BREAKER_LIMIT
    api.breaker_at["anthropic"] = time.time() - api.BREAKER_COOL - 1  # cool-down over: the next request is the probe
    before = time.time()
    server.fail_next = 503
    res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
    assert not res.ok and res.error == "HTTP 503 Service Unavailable" and res.transient
    assert api.breaker_at["anthropic"] >= before  # the cool-down restarts from the failed probe
    server.fail_next = 503
    res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
    assert res.error == "breaker open"
    assert server.fail_next == 503  # no request during the new cool-down


def test_failed_probe_with_a_permanent_answer_also_reopens(server, tmp_path):
    o = agents.Opts(str(tmp_path), prompt_only=True)
    api.breaker["anthropic"] = api.BREAKER_LIMIT
    api.breaker_at["anthropic"] = time.time() - api.BREAKER_COOL - 1
    before = time.time()
    server.fail_next = 400
    res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
    assert not res.ok and not res.transient
    assert api.breaker_at["anthropic"] >= before
    server.fail_next = 503
    res = api.run_api(ANTHROPIC_MODEL, "say hi", o)
    assert res.error == "breaker open"
    assert server.fail_next == 503
