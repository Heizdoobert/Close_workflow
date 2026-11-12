import pytest

from flow_engine import agents, doctor_api

FAKE_KEY = "sk-fake-do-not-print-12345"
API_RUNG = "api:anthropic/claude-haiku-5-5"


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL", "FLOW_POLICY_API_GENERAL", "FLOW_POLICY_API_CODE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_report_shows_key_presence_without_the_value(clean_env):
    clean_env.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    clean_env.delenv("OPENAI_API_KEY", raising=False)
    text = "\n".join(doctor_api.report())
    assert FAKE_KEY not in text
    assert "anthropic" in text and "present" in text
    assert "openai" in text and "missing" in text


def test_report_lists_cli_and_api_modes(clean_env):
    lines = doctor_api.report()
    assert any("cli claude" in line for line in lines)
    assert any("cli agy" in line for line in lines)
    assert any("cli opencode" in line for line in lines)
    assert sum("mode api" in line for line in lines) == 2


def test_policy_api_rung_unset_leaves_chains_identical(clean_env):
    assert agents.chain_from_env("claude:haiku", None, "general") == ["claude:haiku"]
    assert agents.chain_from_env("claude:haiku", None, "code") == ["claude:haiku"]
    chain = agents.chain_from_env("claude:haiku", "gemini-3.8-flash-high", "general")
    assert chain == ["agy:gemini-3.8-flash-high", "agy:gemini-3.8-flash-low"]


def test_policy_api_rung_set_appends_once(clean_env):
    clean_env.setenv("FLOW_POLICY_API_GENERAL", API_RUNG)
    assert agents.chain_from_env("claude:haiku", None, "general") == ["claude:haiku", API_RUNG]
    chain = agents.chain_from_env("claude:haiku", "gemini-3.8-flash-high", "general")
    assert chain == ["agy:gemini-3.8-flash-high", "agy:gemini-3.8-flash-low", API_RUNG]
    clean_env.setenv("FLOW_POLICY_API_GENERAL", "claude:haiku")
    assert agents.chain_from_env("claude:haiku", None, "general") == ["claude:haiku"]


def test_policy_api_rung_is_per_role(clean_env):
    clean_env.setenv("FLOW_POLICY_API_GENERAL", API_RUNG)
    assert agents.chain_from_env("claude:haiku", None, "code") == ["claude:haiku"]


def test_flow_backends_still_wins(clean_env):
    clean_env.setenv("FLOW_BACKENDS", "agy:gpt-oss-120b-medium!")
    clean_env.setenv("FLOW_POLICY_API_GENERAL", API_RUNG)
    assert agents.chain_from_env("claude:haiku", None, "general") == ["agy:gpt-oss-120b-medium!"]
