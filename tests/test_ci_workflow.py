"""Static checks on the CI workflow and dependabot config (stdlib only, no YAML parser)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"


def _ci_text():
    return CI.read_text(encoding="utf-8")


def test_top_level_permissions_are_read_only():
    assert re.search(r"^permissions:\s*\n\s+contents:\s*read\s*$", _ci_text(), re.M)


def test_concurrency_group_cancels_in_progress():
    text = _ci_text()
    assert re.search(r"^concurrency:\s*\n\s+group:\s*.*github\.ref", text, re.M)
    assert re.search(r"^\s+cancel-in-progress:\s*true\s*$", text, re.M)


def test_no_ubuntu_latest_leg_uses_python_39():
    # 3.9 is allowed only on the pinned ubuntu-22.04 leg.
    found = False
    for line in _ci_text().splitlines():
        if re.search(r"""python[":\s]+["']?3\.9["']?""", line):
            found = True
            assert "ubuntu-22.04" in line, line
            assert "ubuntu-latest" not in line, line
    assert found, "expected the ubuntu-22.04 / 3.9 leg to exist"


def test_secrets_job_runs_gitleaks_action():
    text = _ci_text()
    assert re.search(r"^  secrets:\s*$", text, re.M)
    assert "gitleaks/gitleaks-action@v2" in text


def test_dependabot_covers_actions_and_pip_weekly():
    text = DEPENDABOT.read_text(encoding="utf-8")
    assert 'package-ecosystem: "github-actions"' in text
    assert 'package-ecosystem: "pip"' in text
    assert text.count('interval: "weekly"') == 2
