"""Proves the autouse fixture in conftest.py keeps tests out of the real agent-system memory DB."""

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from flow_engine import bridge

REAL_DB_DIR = Path.home() / ".agent_system"
REAL_DB = REAL_DB_DIR / "agent_system.db"


def _stat_or_none(p):
    return (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None


def test_db_dir_is_temp_not_home():
    db_dir = Path(os.environ["AGENT_SYSTEM_DB_DIR"]).resolve()
    assert db_dir != REAL_DB_DIR.resolve()
    assert REAL_DB_DIR.resolve() not in db_dir.parents
    assert Path(tempfile.gettempdir()).resolve() in db_dir.parents


def test_record_recipe_writes_only_to_temp_db(isolated_agent_memory):
    if bridge.agent_system() is None:
        pytest.skip("agent-system CLI not installed")
    before = (REAL_DB_DIR.exists(), _stat_or_none(REAL_DB))
    name = f"isolation-{uuid.uuid4().hex}"

    bridge.record_recipe(name, "isolation probe recipe")

    # The recipe is readable back through the same (temp) DB ...
    assert "isolation probe recipe" in bridge.get_recipe(name)
    # ... something was created under the temp dir ...
    assert any(isolated_agent_memory.iterdir())
    # ... and the real home DB was neither created nor modified.
    assert (REAL_DB_DIR.exists(), _stat_or_none(REAL_DB)) == before
