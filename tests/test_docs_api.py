"""The API mode docs must name the mode and its env vars in both CLAUDE.md and AGENT_SETUP.md."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = [REPO / "CLAUDE.md", REPO / "AGENT_SETUP.md"]
REQUIRED = ["API mode", "ANTHROPIC_API_KEY", "FLOW_API_BASE_URL"]


def test_each_doc_names_api_mode_and_its_env_vars():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for needle in REQUIRED:
            assert needle in text, f"{doc.name} does not mention {needle!r}"


def test_docs_state_that_tool_roles_are_not_served_by_api_mode():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        assert "tool" in text and "CLI" in text, f"{doc.name} does not state the tool-role limit"


SECURITY_AND_LIMITS = [
    "https",
    "localhost, 127.0.0.1 and ::1",
    "Redirects are refused",
    "FLOW_API_MAX_BYTES",
    "4000000",
    "FLOW_API_CACHE_MIN_CHARS",
    "4000",
    "not measured",
]
PROMPT_ONLY_ROLES = ["prompt_only", "review", "SHIP personas", "interview round", "Planner, map, scout and steps"]
TOKEN_SECTION = ["Tokens are the unit of cost", "FLOW_PLAN_MAX_TOKENS", "FLOW_BUDGET_CACHE_READ", "FLOW_TOKEN_CHARS", "FLOW_PRICE_<MODEL>", "## Token tổng"]


def test_each_doc_states_security_rules_and_limits():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for needle in SECURITY_AND_LIMITS:
            assert needle in text, f"{doc.name} does not state {needle!r}"


def test_each_doc_names_the_prompt_only_roles_and_the_missing_tool_loop():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for needle in PROMPT_ONLY_ROLES:
            assert needle in text, f"{doc.name} does not name {needle!r}"
        assert "tool loop" in text, f"{doc.name} does not say the tool loop does not exist"


def test_each_doc_states_the_token_budget_rules():
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for needle in TOKEN_SECTION:
            assert needle in text, f"{doc.name} does not state {needle!r}"
