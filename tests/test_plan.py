import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flow_engine import bridge, engine, plan

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
HERE = Path(__file__).parent


def launcher(d, script):
    import stat
    w = Path(d) / (Path(script).stem + ".sh")
    w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    return str(w)
OSS = "gpt-oss-120b-medium"


def sh(cwd, *a):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sh(repo, "git", "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hello\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "-m", "init")
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "steps.json").write_text("{}")
    for n, v in dict(FLOW_WORKDIR=str(repo), FLOW_HOME=str(home), FAKE_STATE_DIR=str(state), FAKE_STEPS=str(state / "steps.json"),
                     FAKE_PLAN=str(state / "plan.json"), FLOW_CLAUDE=launcher(state, str(HERE / "fake_claude.py")), FLOW_AGY=launcher(state, str(HERE / "fake_agy.py")),
                     FLOW_BACKOFF_BASE="0.001", FLOW_BACKOFF_CAP="0.01", FLOW_PIN_BACKOFF_CAP="0.01", REDIS_PORT="1", FLOW_STALE="1").items():
        monkeypatch.setenv(n, v)
    for k in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL", "FLOW_GATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(engine, "ROOT", home)
    monkeypatch.setattr(engine, "LOGS", home / "logs")
    monkeypatch.setattr(engine, "LOG", home / "logs" / "flow_engine.log")

    class E:
        pass
    e = E()
    e.repo, e.home, e.state = repo, home, state
    e.main_head = sh(repo, "git", "rev-parse", "HEAD")

    def script(steps, behaviour):
        (state / "plan.json").write_text(json.dumps({"steps": steps}))
        (state / "steps.json").write_text(json.dumps(behaviour))
    e.script = script
    e.calls = lambda: [l.split("\t") for l in (state / "calls.log").read_text().splitlines()] if (state / "calls.log").exists() else []
    return e


def step(i, deps=(), files=(), acc=""):
    return {"id": i, "title": f"do {i}", "reason": f"because {i} is needed", "deps": list(deps), "files": list(files), "acceptance": acc, "prompt": f"please do {i}"}


def test_validate_rejects_bad_plans():
    for bad in ({"steps": []}, {"steps": [step("a", deps=["a"])]}, {"steps": [step("a", ["b"]), step("b", ["a"])]},
                {"steps": [step("a", files=["../x"])]}, {"steps": [step("a", files=["/etc/x"])]}, {"steps": [step("a"), step("a")]},
                {"steps": [{**step("a"), "reason": ""}]}):
        with pytest.raises(plan.PlanError):
            plan.validate(bad)
    assert plan.validate({"steps": [step("a"), step("b", ["a"])]})


def test_end_to_end_parallel_steps_merge_and_evidence(env):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'"), step("s2", files=["b.py"], acc="python3 -c 'import b'"),
                step("s3", deps=["s1", "s2"], files=["c.py"], acc="python3 -c 'import a, b, c'")],
               {"s1": {"files": {"a.py": "A = 1\n"}, "sleep": 1}, "s2": {"files": {"b.py": "B = 2\n"}, "sleep": 1},
                "s3": {"files": {"c.py": "import a, b\nC = a.A + b.B\n"}}})
    pid = plan.create("build abc")
    assert plan.status(pid)["status"] == "awaiting_approval" and "do s1" in plan.show(pid)
    assert sh(env.repo, "git", "rev-parse", "HEAD") == env.main_head  # drafting touches nothing
    assert plan.approve(pid, wait_for=True) == "done"
    ev = {c[1]: float(c[2]) for c in env.calls() if c[0] == "step-start"}
    end = {c[1]: float(c[2]) for c in env.calls() if c[0] == "step-end"}
    assert ev["s2"] < end["s1"] and ev["s1"] < end["s2"]            # s1 and s2 really ran at the same time
    assert ev["s3"] >= max(end["s1"], end["s2"])                       # s3 waited for both
    assert sh(env.repo, "git", "show", f"flow/{pid}:c.py").startswith("import a, b")
    assert sh(env.repo, "git", "rev-parse", "HEAD") == env.main_head   # your branch is untouched
    assert not (env.repo / "a.py").exists()
    rep = (env.home / "plans" / pid / "report.md").read_text()
    for needle in ("a.py", "b.py", "c.py", "because s2 is needed", "rc=0", "Kiểm tra tích hợp", "kết quả: OK", f"flow/{pid}"):
        assert needle in rep, needle
    assert json.loads((env.home / "plans" / pid / "report.json").read_text())["state"]["status"] == "done"


def test_503_storm_is_survived_and_shown_in_the_report(env, monkeypatch):
    monkeypatch.setenv("FAKE_503_FIRST", "5")
    monkeypatch.setenv("FLOW_API_RETRIES", "6")
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "x=1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    rep = (env.home / "plans" / pid / "report.md").read_text()
    assert "503" in rep and "retry in" in rep


def test_acceptance_failure_is_fed_back_then_passes(env):
    env.script([step("s1", files=["a.py"], acc="grep -q GOOD a.py")], {"s1": {"files": {"a.py": "BAD\n"}, "fixed_files": {"a.py": "GOOD\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    st = json.loads((env.home / "plans" / pid / "state.json").read_text())["steps"]["s1"]
    assert [a["acceptance"]["ok"] for a in st["attempts"]] == [False, True]
    assert "FAIL" in (env.home / "plans" / pid / "report.md").read_text()


def test_failed_step_skips_dependents_and_plan_is_partial(env, monkeypatch):
    monkeypatch.setenv("FLOW_RETRIES", "1")
    env.script([step("s1", files=["a.py"], acc="false"), step("s2", files=["b.py"], acc="test -f b.py"), step("s3", deps=["s1"], files=["c.py"])],
               {"s1": {"files": {"a.py": "x\n"}}, "s2": {"files": {"b.py": "y\n"}}, "s3": {"files": {"c.py": "z\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "partial"
    st = plan.status(pid)["steps"]
    assert st == {"s1": "failed", "s2": "done", "s3": "skipped"}
    assert sh(env.repo, "git", "ls-tree", "--name-only", f"flow/{pid}").split() == ["README.md", "b.py"]
    rep = (env.home / "plans" / pid / "report.md").read_text()
    assert "a dependency failed" in rep and "chưa commit" in rep and "a.py" in rep   # a failed step still shows what it touched
    assert json.loads((env.home / "plans" / pid / "state.json").read_text())["integration"]["ok"]


def test_merge_conflict_triggers_rescue_rerun(env):
    env.script([step("s1"), step("s2")], {"s1": {"files": {"shared.txt": "one\n"}, "sleep": 0.5}, "s2": {"files": {"shared.txt": "two\n"}, "sleep": 0.5}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    merged = sh(env.repo, "git", "show", f"flow/{pid}:shared.txt")
    assert "one" in merged and "two" in merged                         # neither step's work was lost
    assert "cứu" in (env.home / "plans" / pid / "report.md").read_text()


def test_gpt_oss_parent_runs_the_whole_plan_on_that_model_only(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_503_FIRST", "5")  # hits the planner and the first step
    env.script([step("s1", files=["a.py"], acc="test -f a.py"), step("s2", deps=["s1"], files=["b.py"], acc="test -f b.py")],
               {"s1": {"files": {"a.py": "1\n"}}, "s2": {"files": {"b.py": "2\n"}}})
    pid = plan.create("g", parent_model=OSS)
    assert plan.approve(pid, wait_for=True) == "done"
    calls = env.calls()
    assert {c[1] for c in calls if c[0] == "agy"} == {OSS}
    assert not [c for c in calls if c[0] == "claude"]
    rep = (env.home / "plans" / pid / "report.md").read_text()
    assert OSS in rep and "same conversation" in rep


def test_gpt_oss_dead_key_fails_the_plan_but_never_switches_model(env, monkeypatch):
    monkeypatch.setenv("FAKE_AGY_DEAD", OSS)
    env.script([step("s1")], {"s1": {}})
    with pytest.raises(plan.PlanError, match="planner failed"):
        plan.create("g", parent_model=OSS)
    assert {c[1] for c in env.calls()} == {OSS} and len(env.calls()) == 1


def test_runner_killed_mid_step_is_resumed_by_the_listener_check(env):
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "1\n"}, "sleep": 4}})
    pid = plan.create("g")
    assert plan.approve(pid) == "started"
    sf = env.home / "plans" / pid / "state.json"
    for _ in range(100):
        if json.loads(sf.read_text())["steps"]["s1"]["status"] == "running":
            break
        time.sleep(0.1)
    else:
        pytest.fail("step never started")
    pids = subprocess.run(["pgrep", "-f", f"flow_engine run {pid}"], capture_output=True, text=True).stdout.split()
    assert pids
    for p in pids:
        os.kill(int(p), signal.SIGKILL)
    time.sleep(2.5)                                                    # heartbeat goes stale (FLOW_STALE=1)
    assert json.loads(sf.read_text())["status"] == "running"           # the dead runner left it "running"
    plan.resume_stale()
    for _ in range(300):
        if json.loads(sf.read_text())["status"] in ("done", "failed", "partial"):
            break
        time.sleep(0.2)
    st = json.loads(sf.read_text())
    assert st["status"] == "done", st
    assert "runner died mid-step" in json.dumps(st["steps"]["s1"]["rescues"])
    assert sh(env.repo, "git", "show", f"flow/{pid}:a.py") == "1"


def test_nothing_merged_means_no_fake_integration_ok(env, monkeypatch):
    monkeypatch.setenv("FLOW_RETRIES", "0")
    env.script([step("s1", files=["a.py"], acc="false")], {"s1": {"files": {"a.py": "x\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "failed"
    assert json.loads((env.home / "plans" / pid / "state.json").read_text())["integration"] is None
    assert "Kiểm tra tích hợp" not in (env.home / "plans" / pid / "report.md").read_text()


def test_failed_attempt_records_recipe_and_next_run_reuses_it(env, monkeypatch):
    store, calls = {}, []

    def fake_agent(*args, cwd=None, timeout=60):
        calls.append(args)
        out = ""
        if args[:2] == ("memory", "recipe-record"):
            store[args[3]] = args[5]
        elif args[:2] == ("memory", "recipe-get"):
            out = json.dumps({"recipe": store[args[3]]}) if args[3] in store else "No recipe found for x."
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(bridge, "run_agent", fake_agent)
    env.script([step("s1", files=["a.py"], acc="grep -q GOOD a.py")], {"s1": {"files": {"a.py": "BAD\n"}, "fixed_files": {"a.py": "GOOD\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    st = json.loads((env.home / "plans" / pid / "state.json").read_text())["steps"]["s1"]
    assert st["recipe_recorded"] == "acceptance" and "fixed after 2 attempts" in store["flow-gate:acceptance"]
    assert "recipe_used" not in st  # nothing known the first time
    pid2 = plan.create("g")
    assert plan.approve(pid2, wait_for=True) == "done"
    st2 = json.loads((env.home / "plans" / pid2 / "state.json").read_text())["steps"]["s1"]
    assert st2["recipe_used"] == "acceptance"
    assert "dùng recipe cũ" in (env.home / "plans" / pid2 / "report.md").read_text()


def test_failure_does_not_overwrite_a_recipe_that_worked(env, monkeypatch):
    store = {"flow-gate:acceptance": "worked: use GOOD"}

    def fake_agent(*args, cwd=None, timeout=60):
        out = ""
        if args[:2] == ("memory", "recipe-record"):
            store[args[3]] = args[5]
        elif args[:2] == ("memory", "recipe-get") and args[3] in store:
            out = json.dumps({"recipe": store[args[3]]})
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(bridge, "run_agent", fake_agent)
    env.script([step("s1", files=["a.py"], acc="grep -q GOOD a.py")], {"s1": {"files": {"a.py": "BAD\n"}}})  # never fixed
    pid = plan.create("g")
    plan.approve(pid, wait_for=True)
    st = json.loads((env.home / "plans" / pid / "state.json").read_text())["steps"]["s1"]
    assert st["status"] == "failed" and st["recipe_used"] == "acceptance" and "recipe_recorded" not in st
    assert store["flow-gate:acceptance"] == "worked: use GOOD"


def test_plan_targets_the_callers_repo_and_keeps_it_when_approved_elsewhere(env, monkeypatch, tmp_path):
    monkeypatch.delenv("FLOW_WORKDIR")
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "1\n"}}})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # not a git repo: refuse, never fall back to FLOW_SKILL
    with pytest.raises(plan.PlanError, match="git repo"):
        plan.create("g")
    monkeypatch.chdir(env.repo)
    pid = plan.create("g")
    assert json.loads((env.home / "plans" / pid / "plan.json").read_text())["repo"] == sh(env.repo, "git", "rev-parse", "--show-toplevel")
    monkeypatch.chdir(elsewhere)  # another session approves from another folder
    assert plan.approve(pid, wait_for=True) == "done"
    assert sh(env.repo, "git", "show", f"flow/{pid}:a.py") == "1"


def test_four_plans_at_once_on_one_repo_do_not_collide(env):
    steps = [step("a", files=["a.py"], acc="test -f a.py"), step("b", files=["b.py"], acc="test -f b.py"), step("c", deps=["a"], files=["c.py"], acc="test -f c.py")]
    env.script(steps, {s["id"]: {"files": {s["files"][0]: "x\n"}, "sleep": 1.5} for s in steps})
    pids = [plan.create("g") for _ in range(int(os.environ.get("STRESS_PLANS", "4")))]
    assert len(set(pids)) == len(pids)
    for p in pids:
        assert plan.approve(p) == "started"
    for _ in range(400):
        sts = [json.loads((env.home / "plans" / p / "state.json").read_text())["status"] for p in pids]
        if all(x in ("done", "failed", "partial") for x in sts):
            break
        time.sleep(0.25)
    assert sts == ["done"] * len(pids), [json.loads((env.home / "plans" / p / "state.json").read_text()).get("error") for p in pids]
    for p in pids:
        assert sh(env.repo, "git", "show", f"flow/{p}:c.py") == "x"
    assert sh(env.repo, "git", "rev-parse", "HEAD") == env.main_head and sh(env.repo, "git", "status", "--porcelain") == ""
    assert not list((env.repo / ".git").rglob("*.lock"))


def test_reject_refuses_a_running_plan(env):
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "1\n"}}})
    pid = plan.create("g")
    plan.approve(pid, wait_for=True)
    with pytest.raises(plan.PlanError, match="only a plan awaiting"):
        plan.reject(pid)
