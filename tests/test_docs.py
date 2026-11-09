"""The docs must not claim a fix that has no test: every test name cited in docs/AGENTIC_GAPS.md has to exist, and the usage and CLAUDE.md name the new commands."""

import re
from pathlib import Path

from flow_engine import cli

REPO = Path(__file__).resolve().parents[1]
GAPS = REPO / "docs" / "AGENTIC_GAPS.md"
CLAUDE = REPO / "CLAUDE.md"


def _defined_tests():
    names = set()
    for f in (REPO / "tests").glob("test_*.py"):
        names.update(re.findall(r"^def (test_\w+)\(", f.read_text(encoding="utf-8"), re.M))
    return names


def _cited_tests():
    # backtick spans that are a bare test name; `test_x_*` wildcards and file paths (with / or .) are not names
    spans = re.findall(r"`([^`]+)`", GAPS.read_text(encoding="utf-8"))
    return sorted({s for s in spans if re.fullmatch(r"test_\w+", s)})


def test_every_test_cited_in_the_gap_doc_exists():
    cited = _cited_tests()
    assert len(cited) > 20, "the regex found too few names; the doc format changed"
    missing = [n for n in cited if n not in _defined_tests()]
    assert not missing, f"docs/AGENTIC_GAPS.md cites tests that do not exist: {missing}"


def test_usage_names_recover_and_clean():
    assert "recover" in cli.USAGE
    assert "clean [--older-than DAYS] [--result-branches]" in cli.USAGE


def test_usage_says_doctor_reports_sandbox_and_gitleaks():
    assert "sandbox" in cli.USAGE and "gitleaks" in cli.USAGE


def test_claude_md_phase8_names_recover_clean_bwrap_and_gitleaks():
    text = CLAUDE.read_text(encoding="utf-8")
    phase8 = text.split("## Agentic plan flow", 1)[1]
    for word in ("recover", "clean", "bwrap", "gitleaks"):
        assert word in phase8, f"CLAUDE.md Phase 8 does not mention {word}"
