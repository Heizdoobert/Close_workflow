"""The public repo files must exist and carry their required content: the MIT license, the security reporting path, the changelog and the contribution guide."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(name):
    return (REPO / name).read_text(encoding="utf-8")


def test_license_file_is_mit_for_heizdoobert():
    text = _read("LICENSE")
    assert text.startswith("MIT License")
    assert "Heizdoobert" in text
    assert "Permission is hereby granted, free of charge" in text


def test_security_policy_has_reporting_support_and_response_sections():
    text = _read("SECURITY.md")
    for heading in ("## Reporting a vulnerability", "## Supported versions", "## Response expectation"):
        assert heading in text, f"SECURITY.md is missing {heading!r}"


def test_changelog_follows_keep_a_changelog_with_unreleased_section():
    text = _read("CHANGELOG.md")
    assert "Keep a Changelog" in text
    assert "## [Unreleased]" in text
    assert "## [0.5.0]" in text


def test_contributing_covers_test_command_test_rule_and_acceptance_rule():
    text = _read("CONTRIBUTING.md")
    assert "python3 -m pytest -q" in text
    assert "Every change needs a test" in text
    assert "Acceptance commands must check behaviour" in text


def test_pyproject_declares_mit_license_in_project_table():
    # tomllib is 3.11+ and CI runs 3.10, so read the [project] table as text
    text = _read("pyproject.toml")
    project = re.search(r"^\[project\]\n(.*?)(?=^\[)", text, re.M | re.S)
    assert project, "pyproject.toml has no [project] table"
    assert re.search(r'^license = \{text = "MIT"\}$', project.group(1), re.M)
    assert re.search(r'^name = "flow-engine"$', project.group(1), re.M)
