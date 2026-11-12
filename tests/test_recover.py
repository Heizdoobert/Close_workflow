import datetime
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flow_engine import agents, cli, engine, plan

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="process groups and signals are POSIX")
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def spawned(monkeypatch):
    """Plan ids the recovery asked to start; no real plan runner is spawned."""
    calls = []
    monkeypatch.setattr(plan, "spawn", calls.append)
    return calls


@pytest.fixture
def home(tmp_path, monkeypatch, spawned):
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    monkeypatch.setenv("FLOW_STALE", "1")
    return tmp_path


def make_plan(home, pid, status, heartbeat=None, approved=None, steps=None):
    d = home / "plans" / pid
    d.mkdir(parents=True)
    st = {"id": pid, "status": status, "goal": "g", "steps": steps or {"s1": {"status": "running"}}}
    if heartbeat is not None:
        st["heartbeat"] = heartbeat
    if approved is not None:
        st["approved"] = datetime.datetime.fromtimestamp(approved, datetime.timezone.utc).isoformat()
    (d / "state.json").write_text(json.dumps(st))
    return d


def old(seconds=1000):
    return time.time() - seconds


def alive(pid):
    """Gone = no such pid, or a zombie (killed, its parent already dead, not reaped yet by a PID 1 that does not reap)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True  # no /proc (macOS): kill 0 succeeded, so it is alive


def test_dead_running_plan_is_resumed(home, spawned):
    make_plan(home, "p1", "running", heartbeat=old())
    assert plan.recover() == ["resumed p1 (running, silent for 1000s)"]
    assert spawned == ["p1"]


def test_plan_with_a_live_runner_lock_is_not_resumed(home, spawned):
    d = make_plan(home, "p1", "running", heartbeat=old())
    lk = engine.try_lock(d / "run.lock")  # a live runner holds the lock
    try:
        assert plan.recover() == []
        assert spawned == []
    finally:
        lk.close()


def test_fresh_heartbeat_is_not_resumed(home, spawned):
    make_plan(home, "p1", "running", heartbeat=time.time())
    assert plan.recover() == []
    assert spawned == []


def test_done_plan_is_not_resumed(home, spawned):
    make_plan(home, "p1", "done", heartbeat=old())
    assert plan.recover() == []
    assert spawned == []


def test_approved_plan_with_no_runner_is_resumed_from_its_approval_time(home, spawned):
    make_plan(home, "p1", "approved", approved=old())
    assert plan.recover() == ["resumed p1 (approved, silent for 1000s)"]
    assert spawned == ["p1"]


def test_approved_plan_approved_just_now_is_left_alone(home, spawned):
    make_plan(home, "p1", "approved", approved=time.time())
    assert plan.recover() == []
    assert spawned == []


def test_nothing_to_recover_prints_that(home, capsys):
    assert cli.main(["recover"]) == 0
    assert capsys.readouterr().out == "nothing to recover\n"


def test_cli_recover_prints_one_line_per_plan_acted_on(home, spawned, capsys):
    make_plan(home, "p1", "running", heartbeat=old())
    make_plan(home, "p2", "done", heartbeat=old())
    assert cli.main(["recover"]) == 0
    assert capsys.readouterr().out == "resumed p1 (running, silent for 1000s)\n"


def test_resume_stale_uses_the_same_helper_and_prints_its_lines(home, spawned, capsys):
    make_plan(home, "p1", "approved", approved=old())
    plan.resume_stale()
    assert "resumed p1" in capsys.readouterr().out
    assert spawned == ["p1"]


def test_die_with_runner_is_a_no_op_off_linux(monkeypatch):
    # off Linux there is no libc prctl: the helper must return quietly, never raise
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(agents, "_LIBC", None)
    assert agents._die_with_runner() is None


def test_orphan_agent_group_is_killed_by_recover(home, spawned):
    # a stand-in agent: its command line names claude, so recover treats the group as an agent
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "claude"], start_new_session=True)
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "unrelated"], start_new_session=True)
    try:
        make_plan(home, "p1", "running", heartbeat=old(), steps={"s1": {"status": "running", "agent_pgids": [agent.pid, bystander.pid]}})
        assert plan.recover() == ["resumed p1 (running, silent for 1000s); killed 1 orphan agent group(s)"]
        assert agent.wait(timeout=10) == -signal.SIGKILL
        assert bystander.poll() is None  # a group that is not an agent is never killed
        st = json.loads((home / "plans" / "p1" / "state.json").read_text())
        assert "agent_pgids" not in st["steps"]["s1"]  # forgotten once handled
    finally:
        bystander.kill()
        bystander.wait()
        if agent.poll() is None:
            agent.kill()
            agent.wait()


def test_agent_child_is_killed_when_its_runner_is_sigkilled(tmp_path):
    # the runner is a child python process whose step thread starts an agent; SIGKILL the runner, the agent must follow
    if sys.platform != "linux":
        pytest.skip("PR_SET_PDEATHSIG is Linux-only; macOS relies on flow-task recover")
    pidfile = tmp_path / "agent.pid"
    code = (
        "import threading, time\n"
        "from flow_engine import agents\n"
        "o = agents.Opts(%r, 'Read', 1.0, 60, on_group=lambda g: open(%r, 'w').write(str(g)))\n"
        "threading.Thread(target=agents._run, args=(['sleep', '60'], o), daemon=True).start()\n"
        "time.sleep(120)\n" % (str(tmp_path), str(pidfile))
    )
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    runner = subprocess.Popen([sys.executable, "-c", code], cwd=str(tmp_path), env=env)
    try:
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        else:
            pytest.fail("agent never started")
        agent_pid = int(pidfile.read_text())
        runner.send_signal(signal.SIGKILL)
        runner.wait(timeout=10)
        for _ in range(200):
            if not alive(agent_pid):
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"agent {agent_pid} survived its runner's SIGKILL")
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait()
        try:
            os.killpg(agent_pid, signal.SIGKILL)
        except (NameError, OSError):
            pass


def test_a_runner_that_keeps_dying_is_given_up_on_after_the_cap(home, spawned, monkeypatch):
    monkeypatch.setenv("FLOW_MAX_RESUMES", "2")
    make_plan(home, "pdead", "approved", approved=time.time() - 1000)
    assert len([plan.recover() for _ in range(2)]) == 2 and spawned == ["pdead", "pdead"]
    out = plan.recover()  # the third respawn is refused
    st = json.loads((home / "plans" / "pdead" / "state.json").read_text(encoding="utf-8"))
    assert spawned == ["pdead", "pdead"] and st["status"] == "failed" and "giving up" in st["error"] and out == ["gave up on pdead after 2 resumes"]
    assert plan.recover() == []  # a failed plan is left alone
