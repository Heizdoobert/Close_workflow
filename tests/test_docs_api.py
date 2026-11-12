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
