"""The 12-step method: analysis block (steps 1-6), blast-radius gate (5), review + simplify pass (10-11), coverage table in the report (12)."""

import json
import subprocess
import sys

import pytest

from flow_engine import gate, method, plan
from test_plan import env, step  # noqa: F401  (env is the shared fake-agent fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
GOOD = {"research": [{"file": "README.md", "finding": "the repo only holds a readme"}], "root_cause": "nothing implements the goal yet",
        "approach": "add the files the steps list, one module per step", "alternatives": [{"option": "one big file", "rejected_because": "no parallel steps"}],
        "fix": "small modules, each checked by its own acceptance command", "blast_radius": {"affected": ["README.md"], "risks": "none: only new files are added"},
        "spec": "each step writes its file; the acceptance command imports it"}
EXISTS = {"README.md"}.__contains__


def _read(env, pid, name):
    return (env.home / "plans" / pid / name).read_text(encoding="utf-8")


# --- steps 1-6: the analysis block -----------------------------------------------------------------------------------

def test_a_complete_analysis_has_no_problems():
    assert method.problems(GOOD, EXISTS) == []


@pytest.mark.parametrize("key", ["root_cause", "approach", "fix", "spec", "research", "alternatives", "blast_radius"])
def test_each_missing_part_is_named(key):
    bad = {k: v for k, v in GOOD.items() if k != key}
    assert any(key in p for p in method.problems(bad, EXISTS))


def test_research_must_cite_a_file_that_exists():
    fake = {**GOOD, "research": [{"file": "no/such.py", "finding": "an invented finding about nothing"}]}
    assert any("no/such.py" in p for p in method.problems(fake, EXISTS))
    assert method.problems({**GOOD, "research": [{"file": "./README.md", "finding": "found it in the readme"}]}, EXISTS) == []


def test_no_analysis_at_all_is_one_clear_problem():
    assert len(method.problems(None, EXISTS)) == 1


def test_create_asks_again_then_stores_the_analysis(env, monkeypatch):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    monkeypatch.setenv("FAKE_NO_ANALYSIS", "1")
    with pytest.raises(plan.PlanError, match="planner did not produce a valid plan"):
        plan.create("g")
    monkeypatch.delenv("FAKE_NO_ANALYSIS")
    pid = plan.create("g")
    stored = json.loads(_read(env, pid, "plan.json"))["analysis"]
    assert stored["root_cause"] and stored["blast_radius"]["affected"] == ["README.md"]
    shown = plan.show(pid)
    assert "root cause" in shown and "do s1" in shown  # the analysis does not push the steps out of the review text


def test_step_agents_are_given_the_analysis_and_the_no_rename_rule(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    p = next(x for x in seen if "Your step [" in x)
    assert "Plan analysis (follow it)" in p and "small modules" in p and "Do not rename, move or delete" in p


# --- step 5: blast radius gate ---------------------------------------------------------------------------------------

def _git(root, *a):
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", *a], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "lib.py").write_text("def helper_one():\n    return 1\n\n\nclass Thing:\n    pass\n")
    (tmp_path / "use.py").write_text("from lib import helper_one\n\nprint(helper_one())\n")
    (tmp_path / "old.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_untouched_tree_and_declared_changes_pass(repo):
    assert gate.blast_radius(repo, [])["ok"]
    (repo / "old.txt").unlink()
    assert not gate.blast_radius(repo, [])["ok"]
    assert gate.blast_radius(repo, ["old.txt"])["ok"]  # declared in the step's files


def test_undeclared_rename_is_caught(repo):
    _git(repo, "mv", "old.txt", "new.txt")
    res = gate.blast_radius(repo, ["new.txt"])
    assert not res["ok"] and "old.txt" in res["findings"][0]


def test_removing_a_function_that_is_still_used_is_caught(repo):
    (repo / "lib.py").write_text("class Thing:\n    pass\n")
    res = gate.blast_radius(repo, ["lib.py"])
    assert not res["ok"] and "helper_one" in res["findings"][0] and "use.py" in res["findings"][0]


def test_moving_a_function_to_a_new_file_is_fine(repo):
    (repo / "lib.py").write_text("class Thing:\n    pass\n")
    (repo / "helpers.py").write_text("def helper_one():\n    return 1\n")
    assert gate.blast_radius(repo, ["lib.py", "helpers.py"])["ok"]


def test_removing_an_unused_function_is_fine(repo):
    (repo / "use.py").write_text("print(1)\n")
    (repo / "lib.py").write_text("class Thing:\n    pass\n")
    assert gate.blast_radius(repo, ["lib.py", "use.py"])["ok"]


def test_run_gates_runs_it_only_for_a_plan_step(repo):
    assert "blast_radius" not in gate.run_gates(repo)["gates"]
    assert gate.run_gates(repo, allowed=[])["gates"]["blast_radius"]["ok"]


# --- steps 10-12: review, simplify, report ---------------------------------------------------------------------------

REVIEW = {"review": [{"file": "a.py", "line": 1, "severity": "HIGH", "problem": "no guard for a negative value", "fix": "reject it"},
                     {"file": "not_in_diff.py", "line": 3, "severity": "high", "problem": "invented", "fix": "none"}],
          "simplify": [{"file": "a.py", "what": "inline the constant"}], "edge_cases": ["empty input", "negative value"]}


def _run_plan(env, monkeypatch, review=None):
    if review is not None:
        monkeypatch.setenv("FAKE_REVIEW", review)
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    return pid


def test_review_findings_are_kept_only_for_files_in_the_diff(env, monkeypatch):
    pid = _run_plan(env, monkeypatch, json.dumps(REVIEW))
    rv = json.loads(_read(env, pid, "state.json"))["review"]
    assert rv["status"] == "ok" and [r["file"] for r in rv["review"]] == ["a.py"] and rv["review"][0]["severity"] == "high" and rv["dropped"] == 1
    rep = _read(env, pid, "report.md")
    for needle in ("Độ phủ 12 bước", "no guard for a negative value", "inline the constant", "negative value", "Phân tích (bước 1-6)", "nguyên nhân gốc", "blast_radius qua ở 1/1"):
        assert needle in rep, needle
    assert "invented" not in rep


def test_all_twelve_steps_appear_in_the_coverage_table(env, monkeypatch):
    pid = _run_plan(env, monkeypatch)
    rows = [l for l in _read(env, pid, "report.md").splitlines() if l.startswith("| ") and l.split("|")[1].strip().isdigit()]
    assert [l.split("|")[1].strip() for l in rows] == [str(i) for i in range(1, 13)]
    assert all("THIẾU" not in l for l in rows)


def test_an_unusable_review_reply_does_not_change_the_plan_result(env, monkeypatch):
    pid = _run_plan(env, monkeypatch, "not json at all")
    st = json.loads(_read(env, pid, "state.json"))
    assert st["status"] == "done" and st["review"]["status"] == "not_run"
    rows = [l for l in _read(env, pid, "report.md").splitlines() if l.startswith("| 10 ") or l.startswith("| 11 ")]
    assert len(rows) == 2 and all("THIẾU" in r for r in rows)  # the report says the step did not run


def test_review_can_be_switched_off(env, monkeypatch):
    monkeypatch.setenv("FLOW_REVIEW", "0")
    pid = _run_plan(env, monkeypatch)
    assert json.loads(_read(env, pid, "state.json"))["review"] == {"status": "off"}
    assert not [c for c in env.calls() if c[0] == "review"]


def test_a_plan_without_analysis_still_runs_and_the_report_says_what_was_not_checked(env, monkeypatch):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    d = env.home / "plans" / pid
    p = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    del p["analysis"]  # an older or hand-written plan
    (d / "plan.json").write_text(json.dumps(p), encoding="utf-8")
    assert plan.approve(pid, wait_for=True) == "done"
    rep = _read(env, pid, "report.md")
    assert "không có khối analysis" in rep and "plan không có khối analysis" in rep
