"""Breaker half-open probe: any failed probe re-opens the cool-down from the moment it failed, and one probe at a time."""
import threading
import time
import types

import pytest

from flow_engine import agents, api, apiclient

MODEL = "anthropic/claude-haiku"


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(api, "time", types.SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    api.breaker.clear()
    api.breaker_at.clear()
    yield now
    api.breaker.clear()
    api.breaker_at.clear()


def probe_answers(monkeypatch, clock, error, seconds):
    """The endpoint answers `error` after `seconds` of a slow call."""
    def send(*a, **kw):
        clock[0] += seconds
        raise error
    monkeypatch.setattr(apiclient, "anthropic_call", send)


def half_open(clock):
    api.breaker["anthropic"] = api.BREAKER_LIMIT
    api.breaker_at["anthropic"] = clock[0] - api.BREAKER_COOL - 1


def run(tmp_path):
    return api.run_api(MODEL, "hi", agents.Opts(str(tmp_path), prompt_only=True))


@pytest.mark.parametrize("error", [apiclient.ApiError("HTTP 401", 401), apiclient.ApiError("HTTP 500", 500, transient=True)])
def test_a_slow_failed_probe_reopens_the_cool_down_from_when_it_failed(clock, monkeypatch, tmp_path, error):
    half_open(clock)
    probe_answers(monkeypatch, clock, error, api.BREAKER_COOL + 5)  # the probe outlives the cool-down
    assert run(tmp_path).error == str(error)
    again = run(tmp_path)
    assert again.error == "breaker open" and again.transient


def test_a_permanent_answer_to_the_probe_keeps_the_count_at_the_limit(clock, monkeypatch, tmp_path):
    half_open(clock)
    probe_answers(monkeypatch, clock, apiclient.ApiError("HTTP 400", 400), 1)
    run(tmp_path)
    assert api.breaker["anthropic"] == api.BREAKER_LIMIT


def test_a_permanent_answer_while_closed_does_not_touch_the_breaker(clock, monkeypatch, tmp_path):
    probe_answers(monkeypatch, clock, apiclient.ApiError("HTTP 400", 400), 1)
    run(tmp_path)
    assert "anthropic" not in api.breaker and "anthropic" not in api.breaker_at


def test_only_one_thread_gets_the_probe(clock):
    half_open(clock)
    start, got = threading.Barrier(8), []

    def ask():
        start.wait()
        got.append(api._breaker_allows("anthropic"))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert got.count(True) == 1
