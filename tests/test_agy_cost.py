import stat
import sys

import pytest

from flow_engine import agents

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")

# prints an agy-style JSON answer whose token counts come from FAKE_AGY_IN / FAKE_AGY_OUT, and logs the model it was asked for
FAKE = '''import json, os, sys
with open(os.environ["FAKE_AGY_LOG"], "a") as f:
    f.write(sys.argv[sys.argv.index("--model") + 1] + "\\n")
print(json.dumps({"conversation_id": "c1", "status": "SUCCESS", "response": "done",
                  "usage": {"input_tokens": int(os.environ["FAKE_AGY_IN"]), "output_tokens": int(os.environ["FAKE_AGY_OUT"])}}))
'''


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    script = tmp_path / "fake_agy.py"
    script.write_text(FAKE)
    launcher = tmp_path / "agy"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FLOW_AGY", str(launcher))
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("FAKE_AGY_LOG", str(tmp_path / "agy.log"))
    monkeypatch.setenv("FLOW_CLAUDE", str(tmp_path / "no-claude-here"))  # a fallback to claude would fail loudly
    return tmp_path


def set_tokens(monkeypatch, tokens_in, tokens_out):
    monkeypatch.setenv("FAKE_AGY_IN", str(tokens_in))
    monkeypatch.setenv("FAKE_AGY_OUT", str(tokens_out))


def agy_calls(d):
    p = d / "agy.log"
    return p.read_text().splitlines() if p.exists() else []


def test_known_model_cost_is_nonzero_and_uses_its_rate(fake_agy, monkeypatch):
    set_tokens(monkeypatch, 1_000_000, 1_000_000)
    res = agents.run_agy(agents.GPT_OSS, "hello", agents.Opts(str(fake_agy), budget=5.0))
    assert res.ok and res.cost > 0
    assert res.cost == pytest.approx(0.15 + 0.60)  # 1M input at 0.15 + 1M output at 0.60


def test_unknown_model_uses_fallback_rate(fake_agy, monkeypatch):
    set_tokens(monkeypatch, 1000, 500)
    res = agents.run_agy("mystery-model-9", "hello", agents.Opts(str(fake_agy), budget=5.0))
    assert res.ok
    assert agents.agy_rate("mystery-model-9") == agents.AGY_FALLBACK_RATE
    assert res.cost == pytest.approx((1000 * 5.0 + 500 * 25.0) / 1_000_000)


def test_gemini_flash_matches_its_family_rate():
    assert agents.agy_rate("gemini-3.8-flash-low") == (0.30, 2.50)
    assert agents.agy_rate("GPT-OSS 120B (Medium)") == (0.15, 0.60)


def test_cache_tokens_are_billed_at_the_input_rate():
    cost = agents.agy_cost(agents.GPT_OSS, dict(input_tokens=100, cache_read=200, cache_create=300, output_tokens=0))
    assert cost == pytest.approx(600 * 0.15 / 1_000_000)


def test_over_budget_call_fails_without_retry_or_fallback(fake_agy, monkeypatch):
    set_tokens(monkeypatch, 2_000_000, 0)  # 0.30 USD estimated on the 0.15 rate
    trace = []
    chain = [f"agy:{agents.GPT_OSS}!", "claude:haiku"]  # pinned: a transient error would be retried persistently
    res, spent, _ = agents.call(chain, "hello", agents.Opts(str(fake_agy), budget=0.10), trace, sleep=lambda s: None)
    assert not res.ok and res.over_budget and not res.transient
    assert "budget exceeded" in res.error
    assert len(agy_calls(fake_agy)) == 1  # one call only: no retry
    assert all(t["backend"] != "claude:haiku" for t in trace)  # no fallback to the next backend
    assert trace[-1]["action"] == "over budget: stop"
    assert spent == pytest.approx(res.cost) and spent > 0.10  # the spend is booked


def test_under_budget_call_succeeds(fake_agy, monkeypatch):
    set_tokens(monkeypatch, 1000, 100)
    res, spent, _ = agents.call([f"agy:{agents.GPT_OSS}"], "hello", agents.Opts(str(fake_agy), budget=0.01), [])
    assert res.ok and not res.over_budget
    assert 0 < spent < 0.01
