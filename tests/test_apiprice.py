"""Price table: cost per model from PRICES or FLOW_PRICE_<MODEL>, None for unknown models."""

import os

import pytest

from flow_engine import apiprice

MODEL = "example/test-model"
USAGE = {"input_tokens": 1_000_000, "cache_read": 2_000_000, "cache_create": 500_000, "output_tokens": 100_000}


@pytest.fixture(autouse=True)
def no_price_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("FLOW_PRICE_"):
            monkeypatch.delenv(key)


def test_known_model_cost_matches_hand_computed_value():
    # 1.0*1 + 0.1*2 + 1.25*0.5 + 5.0*0.1 = 1.0 + 0.2 + 0.625 + 0.5
    assert apiprice.cost_of(MODEL, USAGE) == pytest.approx(2.325)


def test_env_override_wins_over_table(monkeypatch):
    monkeypatch.setenv("FLOW_PRICE_EXAMPLE_TEST_MODEL", "2,0.2,2.5,10")
    assert apiprice.price_for(MODEL) == (2.0, 0.2, 2.5, 10.0)
    assert apiprice.cost_of(MODEL, USAGE) == pytest.approx(4.65)


def test_env_override_applies_to_model_not_in_table(monkeypatch):
    monkeypatch.setenv("FLOW_PRICE_OPENAI_GPT_4O_MINI", "0.15,0.075,0,0.6")
    assert apiprice.cost_of("openai/gpt-4o-mini", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == pytest.approx(0.75)


def test_env_override_with_wrong_field_count_raises(monkeypatch):
    monkeypatch.setenv("FLOW_PRICE_EXAMPLE_TEST_MODEL", "1,2,3")
    with pytest.raises(ValueError):
        apiprice.price_for(MODEL)


def test_unknown_model_returns_none():
    assert apiprice.price_for("nobody/unknown-model") is None
    assert apiprice.cost_of("nobody/unknown-model", USAGE) is None


def test_zero_usage_returns_zero():
    zero = {"input_tokens": 0, "cache_read": 0, "cache_create": 0, "output_tokens": 0}
    assert apiprice.cost_of(MODEL, zero) == 0.0
