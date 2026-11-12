"""API calls and the plan's token budget: a budget refusal must not fall to an uncapped CLI backend, and billed tokens of a failed answer still count."""
import pytest

from flow_engine import agents, api, apiclient

MODEL = "openai/example-model"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.delenv("FLOW_PRICE_OPENAI_EXAMPLE_MODEL", raising=False)
    monkeypatch.delenv("FLOW_API_CACHE", raising=False)
    api.breaker.clear()
    api.breaker_at.clear()


def test_a_budget_refusal_does_not_fall_through_to_a_cli_backend(tmp_path, monkeypatch):
    cli = []
    monkeypatch.setitem(agents.BACKENDS, "claude", lambda model, prompt, o: cli.append(prompt) or agents.Result(True, "from the cli"))
    trace = []
    o = agents.Opts(str(tmp_path), prompt_only=True, token_cap=10)  # 10 tokens left: the call cannot fit
    res, _, _ = agents.call([f"api:{MODEL}", "claude:haiku"], "say hi", o, trace, sleep=lambda s: None)
    assert not res.ok and "token budget" in res.error
    assert cli == [] and trace[-1]["action"].startswith("token budget")


def test_without_a_cap_a_failed_api_call_still_falls_through(tmp_path, monkeypatch):
    monkeypatch.setitem(agents.BACKENDS, "claude", lambda model, prompt, o: agents.Result(True, "from the cli"))
    monkeypatch.setattr(apiclient, "openai_call", lambda *a, **k: (_ for _ in ()).throw(apiclient.ApiError("HTTP 404", 404)))
    res, _, _ = agents.call([f"api:{MODEL}", "claude:haiku"], "say hi", agents.Opts(str(tmp_path), prompt_only=True), sleep=lambda s: None)
    assert res.ok and res.text == "from the cli"


@pytest.mark.parametrize("answer", [{"text": "half a sen", "truncated": True}, {"text": "  ", "truncated": False}])
def test_a_truncated_or_empty_answer_still_counts_the_tokens_it_billed(tmp_path, monkeypatch, answer):
    usage = {"input_tokens": 120, "output_tokens": 1000}
    monkeypatch.setattr(apiclient, "openai_call", lambda *a, **k: {**answer, "usage": dict(usage)})
    res, _, tokens = agents.call([f"api:{MODEL}"], "say hi", agents.Opts(str(tmp_path), prompt_only=True), sleep=lambda s: None)
    assert not res.ok and res.error == "empty or truncated answer" and not res.transient
    assert tokens["input_tokens"] == 120 and tokens["output_tokens"] == 1000
