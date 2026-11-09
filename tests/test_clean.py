import datetime
import json
import subprocess
from pathlib import Path

import pytest

from flow_engine import cli, engine, plan


def sh(cwd, *a):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def has_ref(repo, ref):
    return subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=repo, capture_output=True).returncode == 0


def ago(days):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()


def make_repo(path):
    path.mkdir(parents=True)
    sh(path, "git", "init", "-q")
    sh(path, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
    return path


@pytest.fixture
def spawned(monkeypatch):
    """Plan ids the approval asked to start; no real plan runner is spawned."""
    calls = []
    monkeypatch.setattr(plan, "spawn", calls.append)
    return calls


@pytest.fixture
def home(tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(engine, "ROOT", tmp_path / "home")
    return tmp_path / "home"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def make_plan(home, repo, pid, status, files=None, finished=None, wt=False):
    """files: {step id: [paths]}. wt=True also creates the worktrees and branches a run leaves behind."""
    files = files or {"s1": ["a.py"]}
    d = home / "plans" / pid
    d.mkdir(parents=True)
    base = sh(repo, "git", "rev-parse", "HEAD")
    pj = {"goal": "g", "repo": str(repo), "base": base, "steps": [
        {"id": sid, "title": "t", "reason": "r", "prompt": "p", "deps": [], "files": fs, "acceptance": "python3 -m pytest -q"}
        for sid, fs in files.items()]}
    st = {"id": pid, "status": status, "goal": "g", "steps": {sid: {"status": "pending"} for sid in files}}
    if finished:
        st["finished"] = finished
    (d / "plan.json").write_text(json.dumps(pj))
    (d / "state.json").write_text(json.dumps(st))
    if wt:
        (d / "wt").mkdir()
        for sid in files:
            sh(repo, "git", "worktree", "add", "-q", "-b", f"flowstep/{pid}/{sid}", str(d / "wt" / sid), base)
        sh(repo, "git", "worktree", "add", "-q", "-b", f"flow/{pid}", str(d / "wt" / "_plan"), base)
    return d


def test_approve_reports_only_live_plans_on_the_same_repo_that_share_files(home, repo, tmp_path, spawned):
    make_plan(home, repo, "pa", "approved", {"s1": ["a.py", "docs/x.md"]})
    make_plan(home, repo, "pc", "running", {"s1": ["c.py"]})
    make_plan(home, make_repo(tmp_path / "other"), "po", "approved", {"s1": ["a.py"]})  # same path, other repo
    make_plan(home, repo, "pf", "done", {"s1": ["a.py"]}, finished=ago(1))
    make_plan(home, repo, "pb", "awaiting_approval", {"s1": ["./a.py", "b.py"], "s2": ["docs/x.md"]})

    assert plan.approve("pb") == "started"
    assert plan.load("pb")[2]["overlap"] == [{"plan": "pa", "files": ["a.py", "docs/x.md"]}]
    assert spawned == ["pb"]


def test_approve_disjoint_plan_has_no_overlap(home, repo):
    make_plan(home, repo, "pa", "approved", {"s1": ["a.py"]})
    make_plan(home, repo, "pz", "awaiting_approval", {"s1": ["z.py"]})

    plan.approve("pz")
    assert plan.load("pz")[2]["overlap"] == []


def test_cli_warns_on_overlap_before_the_approved_line(home, repo, capsys):
    make_plan(home, repo, "pa", "approved", {"s1": ["a.py"]})
    make_plan(home, repo, "pb", "awaiting_approval", {"s1": ["a.py"]})

    assert cli.main(["approve", "pb"]) == 0
    cap = capsys.readouterr()
    assert "warning: plan pb shares files with plan pa: a.py" in cap.err
    assert cap.out.startswith("approved: running in the background")


def test_clean_removes_worktrees_and_step_branches_and_keeps_result_branch(home, repo):
    d = make_plan(home, repo, "pdone", "done", {"s1": ["a.py"], "s2": ["b.py"]}, finished=ago(0), wt=True)
    (d / "report.md").write_text("report")

    out = plan.clean()

    assert not (d / "wt" / "s1").exists() and not (d / "wt" / "s2").exists() and not (d / "wt" / "_plan").exists()
    assert not has_ref(repo, "refs/heads/flowstep/pdone/s1") and not has_ref(repo, "refs/heads/flowstep/pdone/s2")
    assert has_ref(repo, "refs/heads/flow/pdone")
    assert "pdone: worktree wt/s1" in out and "pdone: branch flowstep/pdone/s2" in out
    assert "pdone: branch flow/pdone" not in out
    assert {p.name for p in d.iterdir()} >= {"plan.json", "state.json", "report.md"}


def test_clean_removes_result_branch_only_when_asked(home, repo):
    make_plan(home, repo, "pres", "failed", finished=ago(0), wt=True)

    out = plan.clean(result_branches=True)

    assert not has_ref(repo, "refs/heads/flow/pres")
    assert "pres: branch flow/pres" in out


@pytest.mark.parametrize("status", ["done", "failed", "partial", "rejected"])
def test_clean_takes_every_finished_status(home, repo, status):
    make_plan(home, repo, "p1", status, finished=ago(0), wt=True)

    plan.clean()
    assert not has_ref(repo, "refs/heads/flowstep/p1/s1")


@pytest.mark.parametrize("status", ["running", "approved", "awaiting_approval"])
def test_clean_never_touches_live_plans(home, repo, status):
    d = make_plan(home, repo, "plive", status, wt=True)

    assert plan.clean() == []
    assert (d / "wt" / "s1").is_dir() and has_ref(repo, "refs/heads/flowstep/plive/s1")


def test_clean_skips_a_plan_whose_runner_still_holds_the_lock(home, repo):
    d = make_plan(home, repo, "pbusy", "done", finished=ago(0), wt=True)
    lock = engine.try_lock(d / "run.lock")
    try:
        assert plan.clean() == []
    finally:
        lock.close()
    assert has_ref(repo, "refs/heads/flowstep/pbusy/s1")


def test_older_than_filters_by_finished_time(home, repo):
    old = make_plan(home, repo, "pold", "done", finished=ago(10), wt=True)
    new = make_plan(home, repo, "pnew", "done", finished=ago(0), wt=True)
    unfinished = make_plan(home, repo, "prej", "rejected", wt=True)  # no finished time: cannot prove it is old enough

    out = plan.clean(older_than_days=5)

    assert not (old / "wt" / "s1").exists() and not has_ref(repo, "refs/heads/flowstep/pold/s1")
    assert (new / "wt" / "s1").is_dir() and has_ref(repo, "refs/heads/flowstep/pnew/s1")
    assert (unfinished / "wt" / "s1").is_dir() and has_ref(repo, "refs/heads/flowstep/prej/s1")
    assert all(line.startswith("pold: ") for line in out)

    plan.clean()  # without the filter the rest goes too
    assert not has_ref(repo, "refs/heads/flowstep/pnew/s1") and not has_ref(repo, "refs/heads/flowstep/prej/s1")


def test_clean_cli_prints_what_was_removed(home, repo, capsys):
    make_plan(home, repo, "pdone", "done", finished=ago(0), wt=True)

    assert cli.main(["clean"]) == 0
    assert "pdone: worktree wt/s1" in capsys.readouterr().out
    assert cli.main(["clean"]) == 0
    assert "nothing to clean" in capsys.readouterr().out
