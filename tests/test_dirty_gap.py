"""P1 #4: approve refuses a repo with uncommitted files unless --allow-dirty, and an accepted dirty list is part of the approval.
Uses the fake agents from test_plan."""
import json

import pytest

from flow_engine import cli, plan
from test_plan import env, step, sh  # noqa: F401  (env is the shared fake-agent fixture)


def _state(e, pid):
    return json.loads((e.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))


def _dirty_plan(e):
    (e.repo / "scratch.txt").write_text("not committed\n")
    e.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "x=1\n"}}})
    return plan.create("g")


def test_approve_refuses_a_dirty_repo_and_lists_the_files(env):
    pid = _dirty_plan(env)
    with pytest.raises(plan.PlanError) as err:
        plan.approve(pid)
    assert "?? scratch.txt" in str(err.value) and "--allow-dirty" in str(err.value)
    st = _state(env, pid)
    assert st["status"] == "awaiting_approval" and "plan_hash" not in st  # nothing was approved or started
    assert not (env.home / "plans" / pid / "run.log").exists()


def test_approve_with_allow_dirty_runs_and_records_the_list(env):
    pid = _dirty_plan(env)
    assert plan.approve(pid, wait_for=True, allow_dirty=True) == "done"
    st = _state(env, pid)
    assert st["dirty_approved"] == ["?? scratch.txt"]
    assert st["plan_hash"] == plan._hash(json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8")), ["?? scratch.txt"])
    assert sh(env.repo, "git", "ls-tree", "--name-only", f"flow/{pid}").split() == ["README.md", "a.py"]  # the steps still run from the commit


def test_the_dirty_list_is_part_of_the_approval_hash(env):
    plan_obj = {"steps": [step("s1", files=["a.py"], acc="test -f a.py")]}
    clean = plan._hash(plan_obj)
    assert plan._hash(plan_obj, []) == clean  # a clean approval keeps the hash it always had
    assert plan._hash(plan_obj, ["?? scratch.txt"]) != clean
    assert plan._hash(plan_obj, ["?? scratch.txt"]) != plan._hash(plan_obj, ["?? other.txt"])


def test_a_plan_approved_with_dirty_files_refuses_to_run_after_the_list_changes(env):
    pid = _dirty_plan(env)
    assert plan.approve(pid, wait_for=True, allow_dirty=True) == "done"
    st = _state(env, pid)
    assert st["dirty_approved"] == ["?? scratch.txt"]
    st["dirty_approved"] = []  # someone edits the recorded approval: the hash no longer matches
    (env.home / "plans" / pid / "state.json").write_text(json.dumps(st), encoding="utf-8")
    with pytest.raises(plan.PlanError, match="changed after approval"):
        plan.run(pid)


def test_cli_approve_refuses_then_allow_dirty_flag_approves(env, capsys):
    pid = _dirty_plan(env)
    assert cli.main(["approve", pid]) == 1
    assert "--allow-dirty" in capsys.readouterr().err
    assert cli.main(["approve", pid, "--wait", "--allow-dirty"]) == 0
    assert _state(env, pid)["dirty_approved"] == ["?? scratch.txt"]


def test_clean_repo_approve_needs_no_flag_and_records_no_dirty_list(env):
    env.script([step("s1", files=["a.py"])], {"s1": {"files": {"a.py": "x=1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    st = _state(env, pid)
    assert "dirty_approved" not in st
    assert st["plan_hash"] == plan._hash(json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8")))
