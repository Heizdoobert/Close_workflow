"""`flow-task selftest` runs the real flow in a throwaway repo; here the agents are the fakes, so this checks the checks and the clean-up."""

import sys
import tempfile

import pytest

from flow_engine import cli, plan, selftest
from test_plan import env, step  # noqa: F401  (env is the shared fake-agent fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
MODULES = {"slug.py": "def slugify(t):\n    return t\n", "wordcount.py": "def top_words(t, n):\n    return []\n", "roman.py": "def to_roman(n):\n    return 'I'\n"}


def script(env):
    steps = [step(f"s{i}", files=[f], acc=f"python3 -c 'import {f[:-3]}'") for i, f in enumerate(MODULES, 1)]
    env.script(steps, {f"s{i}": {"files": {f: src}} for i, (f, src) in enumerate(MODULES.items(), 1)})


def test_selftest_passes_with_working_agents_and_cleans_up(env, capsys):
    script(env)
    assert cli.main(["selftest"]) == 0
    out = capsys.readouterr().out
    assert "selftest PASSED" in out and "FAIL" not in out.replace("FAILED", "")
    for needle in ("every step merged", "integration re-run", "16-step coverage table", "SHIP ran and decided", "your branch was not touched"):
        assert needle in out, needle
    assert not list((env.home / "plans").glob("*"))  # its own plan is removed; --keep leaves it


def test_selftest_fails_loudly_when_a_step_cannot_pass(env, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # --keep leaves its throwaway repo behind: keep it out of /tmp
    steps = [step(f"s{i}", files=[f], acc="python3 -c 'import missing_module'") for i, f in enumerate(MODULES, 1)]
    env.script(steps, {f"s{i}": {"files": {f: src}} for i, (f, src) in enumerate(MODULES.items(), 1)})
    assert cli.main(["selftest", "--keep"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  every step merged" in out and "selftest FAILED" in out
    assert list((env.home / "plans").glob("*"))  # kept for inspection


def test_usage_names_selftest(env):
    assert "selftest" in cli.USAGE and selftest.GOAL.startswith("Add three independent")
