import json
import os
import shlex
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


def step(i, deps=(), files=(), acc=None):
    acc = acc or f"test -f {files[0] if files else 'README.md'}"  # validate() rejects an empty acceptance
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


def test_gemini_parent_only_plans_and_decides_every_hands_on_call_runs_on_gpt_oss(env, monkeypatch):
    gem = "gemini-3.8-flash-high"
    monkeypatch.setenv("FAKE_SCOUT", json.dumps({"facts": [{"file": "a.py", "line": 9, "quote": "value = 1", "note": "the constant"},
                                                           {"file": "a.py", "line": 1, "quote": "value = 2", "note": "not in the file"},
                                                           {"file": "../../etc/passwd", "line": 1, "quote": "root", "note": "outside the worktree"}]}))
    env.script([step("s1", files=["a.py"], acc="test -f a.py"), {**step("s2", deps=["s1"], files=["notes.md"], acc="test -f notes.md"), "kind": "general"}],
               {"s1": {"files": {"a.py": "value = 1\n"}}, "s2": {"files": {"notes.md": "n\n"}}})
    pid = plan.create("g", parent_model=gem)
    assert plan.approve(pid, wait_for=True) == "done"
    # map of the repo (GPT-OSS), planner, both steps (GPT-OSS, even the one marked general), then the scout (GPT-OSS) and the review that decides on its facts
    assert [c[1] for c in env.calls() if c[0] == "agy"] == [OSS, gem, OSS, OSS, OSS, gem]
    assert [c for c in env.calls() if c[0] == "review"] == [["review", "scouted"]]
    st = json.loads((env.home / "plans" / pid / "state.json").read_text())
    assert st["scout"] == {"status": "ok", "facts": [{"file": "a.py", "line": 1, "quote": "value = 1", "note": "the constant"}], "dropped": 2}  # only the quote that is really in the file
    assert [(c["role"], c["backend"]) for c in st["calls"]] == [("map", f"agy:{OSS}"), ("planner", f"agy:{gem}"), ("step:s1", f"agy:{OSS}"), ("step:s2", f"agy:{OSS}"), ("scout", f"agy:{OSS}"), ("review", f"agy:{gem}")]
    assert "| scout | agy:gpt-oss-120b-medium | 1 | 7 |" in (env.home / "plans" / pid / "report.md").read_text()
    for k, v in st["tokens"].items():  # the totals count the map and planner calls too: they agree with the ledger rows
        assert v == sum(c.get(k, 0) for c in st["calls"]), k
    assert st["tokens"]["input_tokens"] > 0
    assert [s["kind"] for s in json.loads((env.home / "plans" / pid / "plan.json").read_text())["steps"]] == ["code", "general"]  # a missing or unknown kind means code


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


@pytest.mark.parametrize("gitignore", ["", ".pytest_cache\n__pycache__\n"])
def test_step_that_runs_pytest_is_committed_with_or_without_gitignore(env, gitignore):
    """Regression: `git add` with an exclude-pathspec failed when .gitignore listed .pytest_cache, so every pytest step was marked failed."""
    if gitignore:
        (env.repo / ".gitignore").write_text(gitignore)
        sh(env.repo, "git", "add", ".gitignore")
        sh(env.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore")
    env.script([step("s1", files=["a.py"], acc="test -f a.py")],
               {"s1": {"files": {"a.py": "1\n", ".pytest_cache/v/x": "junk\n", "__pycache__/a.cpython-311.pyc": "junk\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    tree = sh(env.repo, "git", "ls-tree", "-r", "--name-only", f"flow/{pid}")
    assert "a.py" in tree.split() and "pytest_cache" not in tree and ".pyc" not in tree


def test_retry_reruns_only_the_failed_steps_of_a_failed_plan(env, monkeypatch):
    monkeypatch.setenv("FLOW_RETRIES", "0")
    env.script([step("a", files=["a.py"], acc="grep -q GOOD a.py"), step("b", files=["b.py"], acc="test -f b.py")],
               {"a": {"files": {"a.py": "BAD\n"}}, "b": {"files": {"b.py": "1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "partial"
    with pytest.raises(plan.PlanError, match="not awaiting approval"):  # approve is not the way back in
        plan.approve(pid)
    before = len(env.calls())
    env.script([step("a", files=["a.py"], acc="grep -q GOOD a.py"), step("b", files=["b.py"], acc="test -f b.py")], {"a": {"files": {"a.py": "GOOD\n"}}, "b": {"files": {"b.py": "2\n"}}})
    plan.retry(pid)
    sf = env.home / "plans" / pid / "state.json"
    for _ in range(200):
        st = json.loads(sf.read_text())
        if st["status"] in ("done", "failed", "partial"):
            break
        time.sleep(0.2)
    assert st["status"] == "done", st
    assert st["retries"] == 1 and st["steps"]["a"]["previous"]["status"] == "failed"
    assert sh(env.repo, "git", "show", f"flow/{pid}:b.py") == "1"  # step b was kept, not re-run
    assert [c[1] for c in env.calls()[before:] if c[0] == "step-start"] == ["a"]  # only step a ran again

def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie has exited; only its parent has not reaped it yet
    except OSError:  # gone between open() and read() (ProcessLookupError), or no /proc at all
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_dead(pid):
    deadline = time.time() + 5
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def test_acceptance_timeout_kills_the_hung_grandchild_too(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_ACCEPT_TIMEOUT", "1")
    pidfile = tmp_path / "gc.pid"
    r = plan.accept({"acceptance": f"sleep 60 & echo $! > {pidfile}; wait"}, str(tmp_path))
    assert r["ok"] is False and r["rc"] is None and r["tail"] == "timeout"
    assert _wait_dead(int(pidfile.read_text())), "a grandchild of a timed-out acceptance command survived"


def test_acceptance_grandchild_holding_output_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_ACCEPT_TIMEOUT", "1")
    pidfile = tmp_path / "gc.pid"
    t0 = time.time()
    r = plan.accept({"acceptance": f"sleep 60 & echo $! > {pidfile}; sleep 60"}, str(tmp_path))
    assert r["tail"] == "timeout" and time.time() - t0 < 15
    assert _wait_dead(int(pidfile.read_text()))


def test_acceptance_huge_output_is_capped_and_keeps_the_end(tmp_path):
    py = shlex.quote(sys.executable)
    r = plan.accept({"acceptance": f"{py} -c \"import sys; sys.stdout.write('x' * (8 * 1024 * 1024) + 'END\\n'); sys.stderr.write('ERR\\n')\""}, str(tmp_path))
    assert r["ok"] is True and r["rc"] == 0
    assert len(r["tail"]) <= 600 and r["tail"].endswith("END\nERR")


def test_acceptance_failure_still_reports_rc_and_tail(tmp_path):
    py = shlex.quote(sys.executable)
    r = plan.accept({"acceptance": f"{py} -c \"import sys; print('nope'); sys.exit(3)\""}, str(tmp_path))
    assert r["ok"] is False and r["rc"] == 3 and r["tail"] == "nope"


def test_output_tail_reads_at_most_the_cap(tmp_path):
    f = tmp_path / "big"
    f.write_bytes(b"a" * (1024 * 1024) + b"TAIL")
    with open(f, "rb") as fh:
        t = plan._tail(fh)
    assert len(t) <= plan.ACCEPT_TAIL_BYTES and t.endswith("TAIL")


@pytest.mark.parametrize("path", ["CON", "con.txt", "PRN.py", "aux/x.py", "NUL", "nul.tar.gz", "COM1", "com9.log", "LPT1.txt", "lpt9", "dir/CON.md"])
def test_validate_rejects_windows_reserved_device_names(path):
    with pytest.raises(plan.PlanError, match="unsafe path"):
        plan.validate({"steps": [step("a", files=[path])]})


@pytest.mark.parametrize("path", ["a<b.py", "a>b", "a:b", 'a"b', "a|b", "a?b", "a*b", "dir:stream/x.py", "x\\y:z.py"])
def test_validate_rejects_windows_invalid_characters(path):
    with pytest.raises(plan.PlanError, match="unsafe path"):
        plan.validate({"steps": [step("a", files=[path])]})


@pytest.mark.parametrize("path", ["dir./x.py", "file.py.", "file.py ", "dir /x.py", "a/b. /c"])
def test_validate_rejects_components_ending_in_dot_or_space(path):
    with pytest.raises(plan.PlanError, match="unsafe path"):
        plan.validate({"steps": [step("a", files=[path])]})


def test_validate_rejects_backslash_traversal_on_any_os():
    with pytest.raises(plan.PlanError, match="unsafe path"):
        plan.validate({"steps": [step("a", files=["..\\secret.txt"])]})


@pytest.mark.parametrize("path", ["", "   ", "\t"])
def test_validate_rejects_empty_or_whitespace_paths_and_ids(path):
    with pytest.raises(plan.PlanError):
        plan.validate({"steps": [step("a", files=[path])]})
    with pytest.raises(plan.PlanError):
        plan.validate({"steps": [step(path or " ")]})


def test_validate_keeps_ordinary_paths_working():
    ok = ["flow_engine/plan.py", "tests/test_plan.py", "CONFIG.py", "COM10.txt", "console.py", "a.b/c-d_e.txt", "./x.py", "docs/AGENTIC_GAPS.md", "auxiliary/nullable.py"]
    assert plan.validate({"steps": [step("a", files=ok)]})


def test_symlink_pointing_outside_the_worktree_is_detected(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (wt / "ok.py").write_text("x = 1\n")
    (wt / "inner").symlink_to(wt / "ok.py")
    (wt / "link").symlink_to(outside)
    (wt / "dir_link").symlink_to(tmp_path)
    assert plan.escaping_paths(str(wt), ["ok.py", "inner", "new.py", "link", "dir_link/outside.txt"]) == ["link", "dir_link/outside.txt"]


FAKE_KEY = "sk-ant-FAKE1234567890ABCDEF"
FAKE_AWS = "AKIAFAKEFAKEFAKE1234"


def test_redact_masks_each_secret_shape_and_keeps_the_words_around_it():
    cases = {
        f"key {FAKE_KEY} here": "key [REDACTED] here",
        "openai sk-FAKE1234567890ABCDEFGH": "openai [REDACTED]",
        f"aws {FAKE_AWS} id": "aws [REDACTED] id",
        "gh ghp_FAKE1234567890ABCDEF ok": "gh [REDACTED] ok",
        "pat github_pat_FAKE1234567890ABCDEFGH ok": "pat [REDACTED] ok",
        "Authorization: Bearer FAKEbearerTOKEN123": "Authorization: Bearer [REDACTED]",
        "api_key = FakeValue123": "api_key = [REDACTED]",
        '"password": "FakePass987"': '"password": "[REDACTED]"',
        "-----BEGIN RSA PRIVATE KEY-----\nFAKEKEYMATERIAL\n-----END RSA PRIVATE KEY-----": "[REDACTED]",  # gitleaks:allow fake PEM fixture
        "-----BEGIN PRIVATE KEY-----\nFAKEKEYMATERIAL": "[REDACTED]",  # truncated block still masked
        "tokens are fine, the task-runner too": "tokens are fine, the task-runner too",
    }
    for raw, want in cases.items():
        assert plan.redact(raw) == want
        assert plan.redact(plan.redact(raw)) == want  # idempotent
    assert plan.redact(None) == ""


def test_secret_in_agent_summary_and_acceptance_output_is_redacted_in_state_and_report(env, monkeypatch):
    real_call = plan.agents.call

    def leaky_call(*args, **kwargs):
        res, cost, tok = real_call(*args, **kwargs)
        res.text += f" leaked {FAKE_KEY} and {FAKE_AWS}"
        return res, cost, tok
    monkeypatch.setattr(plan.agents, "call", leaky_call)
    check = f"import sys; sys.stderr.write('password=FakePass987 {FAKE_AWS}\\n'); sys.exit(1)"
    env.script([step("s1", files=["a.py"], acc=f"{shlex.quote(sys.executable)} -c {shlex.quote(check)}")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    plan.approve(pid, wait_for=True)
    d = env.home / "plans" / pid
    for name in ("report.md", "report.json", "state.json"):
        text = (d / name).read_text(encoding="utf-8")
        assert FAKE_KEY not in text and FAKE_AWS not in text and "FakePass987" not in text, name
        assert "leaked" in text and "[REDACTED]" in text, name  # the words around the secret survive
    assert "password=[REDACTED]" in (d / "state.json").read_text(encoding="utf-8")
    assert json.loads((d / "state.json").read_text(encoding="utf-8"))["steps"]["s1"]["summary"].endswith("leaked [REDACTED] and [REDACTED]")


def test_secret_in_failure_recipe_is_redacted_before_it_is_stored(env, monkeypatch):
    store = {}

    def fake_agent(*args, cwd=None, timeout=60):
        out = ""
        if args[:2] == ("memory", "recipe-record"):
            store[args[3]] = args[5]
        elif args[:2] == ("memory", "recipe-get"):
            out = "No recipe found for x."
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(bridge, "run_agent", fake_agent)
    check = f"import sys; sys.stderr.write('token={FAKE_AWS}\\n'); sys.exit(1)"
    env.script([step("s1", files=["a.py"], acc=f"{shlex.quote(sys.executable)} -c {shlex.quote(check)}")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    plan.approve(pid, wait_for=True)
    recipe = store["flow-gate:acceptance"]
    assert FAKE_AWS not in recipe and "token=[REDACTED]" in recipe and "still failing" in recipe


def test_budget_reserve_keeps_parallel_steps_within_plan_budget(env, monkeypatch):
    # each fake call costs $0.001; with $0.0015 for the whole plan only one $0.001 reservation fits at a time
    monkeypatch.setenv("FLOW_PLAN_MAX_USD", "0.0015")
    monkeypatch.setenv("FLOW_STEP_USD", "0.001")
    env.script([step("s1", files=["a.py"]), step("s2", files=["b.py"])],
               {"s1": {"files": {"a.py": "A = 1\n"}, "sleep": 1}, "s2": {"files": {"b.py": "B = 2\n"}, "sleep": 1}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "partial"
    st = json.loads((env.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))
    assert st["cost_usd"] <= 0.0015 + 1e-9
    assert [st["steps"][s]["status"] for s in ("s1", "s2")] == ["done", "failed"]
    assert "cannot cover the per-step reserve" in st["steps"]["s2"]["error"]
    assert st["steps"]["s2"].get("attempts", []) == []  # s2 was never launched, so it paid nothing
    assert {c[1] for c in env.calls() if c[0] == "step-start"} == {"s1"}


def test_budget_big_enough_for_both_parallel_steps_runs_both(env, monkeypatch):
    monkeypatch.setenv("FLOW_PLAN_MAX_USD", "0.002")
    monkeypatch.setenv("FLOW_STEP_USD", "0.001")
    env.script([step("s1", files=["a.py"]), step("s2", files=["b.py"])],
               {"s1": {"files": {"a.py": "A = 1\n"}, "sleep": 1}, "s2": {"files": {"b.py": "B = 2\n"}, "sleep": 1}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    st = json.loads((env.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))
    assert st["cost_usd"] <= 0.002 + 1e-9


def test_dirty_repo_plan_warns_and_runs_from_the_commit(env):
    (env.repo / "scratch.txt").write_text("not committed\n")
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "x=1\n"}}})
    pid = plan.create("g")
    st = json.loads((env.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))
    assert st["dirty_files"] == ["?? scratch.txt"]
    assert json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8"))["dirty_files"] == ["?? scratch.txt"]
    md = (env.home / "plans" / pid / "plan.md").read_text(encoding="utf-8")
    assert "WARNING" in md and "?? scratch.txt" in md
    assert plan.approve(pid, wait_for=True) == "done"
    assert sh(env.repo, "git", "ls-tree", "--name-only", f"flow/{pid}").split() == ["README.md", "a.py"]  # scratch.txt is not merged


def test_clean_repo_has_no_dirty_warning(env):
    env.script([step("s1", files=["a.py"])], {"s1": {"files": {"a.py": "x=1\n"}}})
    pid = plan.create("g")
    assert "dirty_files" not in json.loads((env.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))
    assert "WARNING" not in (env.home / "plans" / pid / "plan.md").read_text(encoding="utf-8")


def test_repo_with_no_commits_is_refused_with_a_clear_message(tmp_path, env, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    sh(empty, "git", "init", "-q", "-b", "main")
    monkeypatch.setenv("FLOW_WORKDIR", str(empty))
    env.script([step("s1", files=["a.py"])], {"s1": {"files": {"a.py": "x=1\n"}}})
    with pytest.raises(plan.PlanError, match="repo has no commits: make an initial commit first"):
        plan.create("g")
    assert not (env.home / "plans").exists() or not any((env.home / "plans").iterdir())  # nothing half-created


def test_detached_head_plan_is_created_from_the_commit_sha(env):
    sh(env.repo, "git", "checkout", "-q", "--detach")
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "x=1\n"}}})
    pid = plan.create("g")
    d = json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8"))
    assert d["base"] == env.main_head and len(d["base"]) == 40
    assert plan.approve(pid, wait_for=True) == "done"
    assert sh(env.repo, "git", "rev-parse", f"flow/{pid}^1") == env.main_head


def _approved_not_started(env, monkeypatch, acc="test -f a.py"):
    monkeypatch.setattr(plan, "spawn", lambda pid: None)
    env.script([step("s1", files=["a.py"], acc=acc)], {"s1": {"files": {"a.py": "1\n"}}})
    pid = plan.create("g")
    plan.approve(pid)
    return pid


def test_plan_edited_after_approval_is_refused(env, monkeypatch):
    pid = _approved_not_started(env, monkeypatch)
    pf = env.home / "plans" / pid / "plan.json"
    p = json.loads(pf.read_text())
    p["steps"][0]["acceptance"] = "echo pwned"
    pf.write_text(json.dumps(p))
    with pytest.raises(plan.PlanError, match="changed after approval"):
        plan.run(pid)
    st = json.loads((env.home / "plans" / pid / "state.json").read_text())
    assert st["status"] == "failed" and not [c for c in env.calls() if c[0] == "step-start"]  # nothing ran, and resume_stale will not respawn it


def test_step_left_queued_by_a_killed_runner_is_run_again(env, monkeypatch):
    pid = _approved_not_started(env, monkeypatch)
    sf = env.home / "plans" / pid / "state.json"
    st = json.loads(sf.read_text())
    st["steps"]["s1"]["status"] = "queued"
    sf.write_text(json.dumps(st))
    assert plan.run(pid) == "done"


def test_acceptance_command_gets_no_credentials_from_the_environment(env, monkeypatch):
    monkeypatch.setenv("SECRET_PROBE_TOKEN", "hunter2hunter2")
    monkeypatch.setenv("KEEP_ME", "ok")
    pid = _approved_not_started(env, monkeypatch, acc='test -z "$SECRET_PROBE_TOKEN" && test "$KEEP_ME" = ok && test -f a.py')
    assert plan.run(pid) == "done"


def test_acceptance_command_keeps_the_git_config_pairs_of_the_runner(env, monkeypatch):
    for k, v in (("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_KEY_0", "core.fileMode"), ("GIT_CONFIG_VALUE_0", "false")):
        monkeypatch.setenv(k, v)
    pid = _approved_not_started(env, monkeypatch, acc='test "$(git config --get core.fileMode)" = false && test -f a.py')
    assert plan.run(pid) == "done"


def test_agent_timeout_kills_the_agents_children_too(tmp_path):
    from flow_engine import agents
    pidfile = tmp_path / "pid"
    o = agents.Opts(str(tmp_path), "Read", 0.1, 1)
    with pytest.raises(subprocess.TimeoutExpired):
        agents._run(["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], o)
    time.sleep(0.3)
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the grandchild is gone, not orphaned


def test_gate_tool_that_hangs_becomes_a_failed_gate_not_a_crash():
    from flow_engine import gate
    r = gate._run(".", ["sleep", "30"], timeout=1)
    assert r.returncode == 124 and "timeout" in r.stderr


def test_a_runner_that_cannot_start_fails_the_plan_instead_of_leaving_it_approved(env, monkeypatch):
    pid = _approved_not_started(env, monkeypatch)
    real = plan.git

    def broken(cwd, *a, **k):
        if a[:2] == ("worktree", "add"):
            raise RuntimeError("cannot create the worktree")
        return real(cwd, *a, **k)
    monkeypatch.setattr(plan, "git", broken)
    assert plan.run(pid) == "failed"
    st = json.loads((env.home / "plans" / pid / "state.json").read_text())
    assert st["status"] == "failed" and "runner could not start" in st["error"]  # not 'approved': recover() would respawn it forever


def test_retry_after_clean_continues_on_the_kept_result_branch(env, monkeypatch):
    monkeypatch.setenv("FLOW_RETRIES", "0")
    two = [step("a", files=["a.py"], acc="grep -q GOOD a.py"), step("b", files=["b.py"], acc="test -f b.py")]
    env.script(two, {"a": {"files": {"a.py": "BAD\n"}}, "b": {"files": {"b.py": "1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "partial"
    assert any(f"{pid}: worktree wt/_plan" in line for line in plan.clean())  # the plan worktree goes, the branch flow/<pid> stays
    env.script(two, {"a": {"files": {"a.py": "GOOD\n"}}, "b": {"files": {"b.py": "1\n"}}})
    plan.retry(pid)
    sf = env.home / "plans" / pid / "state.json"
    for _ in range(200):
        st = json.loads(sf.read_text())
        if st["status"] in ("done", "failed", "partial"):
            break
        time.sleep(0.2)
    assert st["status"] == "done", st
