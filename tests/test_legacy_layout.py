"""The unused root files live in legacy/ and nothing tracked refers to them by their old path."""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOVED = ["api_service.py", "generated_api.py", "close_workflow_cli.py", "closed_loop_task_engine.json",
         "soul_canonical_15nt.md", "test_report.md", "test_system.sh"]


def _tracked():
    return subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()


def test_the_root_no_longer_holds_the_legacy_files():
    assert not [f for f in MOVED if (REPO / f).exists()]


def test_legacy_holds_every_file_and_a_readme_that_names_each():
    readme = (REPO / "legacy" / "README.md").read_text(encoding="utf-8")
    for f in MOVED:
        assert (REPO / "legacy" / f).is_file(), f
        assert f in readme, f


def test_no_tracked_code_outside_legacy_imports_a_moved_module():
    pat = re.compile(r"^\s*(import|from)\s+(api_service|generated_api|close_workflow_cli)\b", re.M)
    for f in _tracked():
        if f.endswith((".py", ".yml", ".yaml", ".sh", ".toml")) and not f.startswith("legacy/") and not f.startswith("tests/test_legacy_layout"):
            assert not pat.search((REPO / f).read_text(encoding="utf-8", errors="ignore")), f
