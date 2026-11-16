"""The bench section in AGENT_SETUP.md and CLAUDE.md must name the four commands, the harness layout and the NEXT line."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCS = [REPO / "AGENT_SETUP.md", REPO / "CLAUDE.md"]
COMMANDS = [
    "flow-task bench run",
    "flow-task bench compare",
    "flow-task bench analyze",
    "flow-task bench change",
]
PATHS = ["harness/bench", "harness/manifest"]


def _text(path):
    return path.read_text(encoding="utf-8")


def test_each_bench_command_is_documented_in_both_files():
    for doc in DOCS:
        text = _text(doc)
        for cmd in COMMANDS:
            assert cmd in text, f"{doc.name} does not mention '{cmd}'"


def test_bench_paths_are_documented_in_both_files():
    for doc in DOCS:
        text = _text(doc)
        for path in PATHS:
            assert path in text, f"{doc.name} does not mention '{path}'"


def test_next_line_is_documented_in_both_files():
    for doc in DOCS:
        assert "NEXT:" in _text(doc), f"{doc.name} does not mention the 'NEXT:' line"
