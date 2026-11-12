"""Holes found by the architecture-fit review: a planner answer of the wrong type, an interview number that is not finite, a cache key that crashed on bytes, and an API rung that sat behind a pin."""
import subprocess
import sys

import pytest

from flow_engine import agents, api, apiclient, interview, plan

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX git fixtures")


def step(**over):
    s = {"id": "s1", "title": "t", "reason": "r", "prompt": "p", "files": ["a.py"], "deps": [], "acceptance": "python3 -m pytest -q tests/test_a.py"}
    s.update(over)
    return {"steps": [s]}


@pytest.mark.parametrize("field,value", [("files", "a.py"), ("files", 5), ("files", {"a": 1}), ("deps", 1), ("deps", "s2"), ("files", [{"a": 1}])])
def test_a_planner_answer_of_the_wrong_type_is_a_plan_error(field, value):
    with pytest.raises(plan.PlanError, match=field):
        plan.validate(step(**{field: value}))


@pytest.mark.parametrize("value", [None, [], ""])
def test_an_empty_files_or_deps_is_still_fine(value):
    out = plan.validate(step(files=value, deps=value))
    assert out["steps"][0]["files"] == [] and out["steps"][0]["deps"] == []


@pytest.mark.parametrize("number", ["Infinity", "-Infinity", "NaN", "1e999"])
def test_an_interview_confidence_that_is_not_finite_is_an_interview_error(number):
    reply = '{"confidence": %s, "question": "q?", "guess": "g", "hypothesis": "h"}' % number
    with pytest.raises(interview.InterviewError, match="confidence"):
        interview._parse(reply, False)


def git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True, env=api.git_env())


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_bytes(b"ok\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_cache_key_survives_a_file_that_is_not_utf8(repo):
    (repo / "a.txt").write_bytes(b"caf\xe9 \xff\xfe\n")  # a tracked file edited in cp1258 / latin-1: git diff prints those bytes
    key = api._cache_key(str(repo), "openai/m", "prompt")
    assert isinstance(key, str) and len(key) == 64


def test_cache_key_still_changes_with_the_tree(repo):
    before = api._cache_key(str(repo), "openai/m", "prompt")
    (repo / "a.txt").write_bytes(b"changed\n")
    assert api._cache_key(str(repo), "openai/m", "prompt") != before


def test_a_run_with_the_cache_on_does_not_crash_on_a_non_utf8_edit(repo, monkeypatch):
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("FLOW_API_CACHE", "1")
    monkeypatch.setattr(apiclient, "openai_call", lambda *a, **k: calls.append(1) or {"text": "hi", "truncated": False, "usage": {}})
    api.cache.clear()
    (repo / "a.txt").write_bytes(b"caf\xe9 \xff\xfe\n")
    o = agents.Opts(str(repo), prompt_only=True)
    first, second = api.run_api("openai/m", "say hi", o), api.run_api("openai/m", "say hi", o)
    assert first.ok and second.ok and calls == [1]


RUNG = "api:openai/example-model"


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL", "FLOW_POLICY_API_GENERAL", "FLOW_POLICY_API_CODE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_an_api_rung_is_not_queued_behind_a_pinned_model(clean_env):
    clean_env.setenv("FLOW_POLICY_API_CODE", RUNG)
    assert agents.chain_from_env(parent_model="gpt-oss-120b-medium", role="code") == ["agy:gpt-oss-120b-medium!"]


def test_an_api_rung_still_ends_a_chain_that_can_fall_back(clean_env):
    clean_env.setenv("FLOW_POLICY_API_GENERAL", RUNG)
    chain = agents.chain_from_env(parent_model="gemini-3.8-flash-high", role="general")
    assert chain[-1] == RUNG and not any(s.endswith("!") for s in chain)
    assert agents.chain_from_env(role="general")[-1] == RUNG  # no parent: the default backend, then the rung
