import json
import os
import stat
import subprocess
import sys
import time

import pytest

from flow_engine import agents, bridge, cli, engine, gate
from flow_engine.resp import Redis, RedisError

posix = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")


def test_resp_unicode_and_auth(fake_redis):
    r = engine.redis()
    r.call("SET", "k", "tiếng Việt ✓\nline2")
    assert r.call("GET", "k") == "tiếng Việt ✓\nline2"
    with pytest.raises(RedisError):
        Redis("127.0.0.1", fake_redis.server_address[1], "wrong").call("PING")


def test_submit_enqueue_status(fake_redis):
    tid = engine.enqueue("fix the bug")
    assert json.loads(fake_redis.db[f"task:{tid}"])["status"] == "queued"
    assert (engine.INCOMING / f"{tid}.txt").read_text(encoding="utf-8") == "fix the bug"
    assert not list(engine.INCOMING.glob("*.tmp"))
    assert engine.status(tid)["status"] == "queued"
    assert engine.status("nope")["status"] == "unknown"


def _script(path, body):
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@posix
def test_execute_retries_on_gate_failure_and_learns(fake_redis, tmp_path, monkeypatch):
    calls = tmp_path / "agent_calls.txt"
    prompts = tmp_path / "prompts.txt"
    monkeypatch.setenv("FLOW_CLAUDE", _script(tmp_path / "claude", (
        "import sys, json\n"
        f"open({str(prompts)!r}, 'a').write(sys.stdin.read() + '\\n=====\\n')\n"
        "print(json.dumps({'type':'result','result':'patched','total_cost_usd':0.001,'usage':{'output_tokens':5}}))\n")))
    agent = _script(tmp_path / "agent-system", f"import sys\nopen({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\nprint('[]')\n")
    monkeypatch.setattr(bridge, "agent_system", lambda: agent)
    monkeypatch.setenv("FLOW_GATE", "1")
    monkeypatch.setenv("FLOW_WORKDIR", str(tmp_path))
    results = iter([{"ok": False, "gates": {"tests": {"ok": False}}}, {"ok": True, "gates": {"tests": {"ok": True}}}])
    monkeypatch.setattr(gate, "run_gates", lambda root: next(results))

    ok, rec = engine.execute("77", "add feature")
    assert ok and rec["attempts"] == 2
    assert prompts.read_text().count("=====") == 2 and "failed these automated checks" in prompts.read_text()
    log = calls.read_text()
    assert "memory search" in log                      # recall before the task
    assert "recipe-record --key flow-gate:tests" in log  # lesson from the retry
    assert "memory index --id flow-77" in log           # learn after success
    st = fake_redis.db["taskstate:77"]
    assert st["status"] == "done" and st["attempts"] == "2"


@posix
def test_execute_without_agent_system_still_works(fake_redis, tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_CLAUDE", _script(tmp_path / "claude", "import json\nprint(json.dumps({'type':'result','result':'ok','total_cost_usd':0}))\n"))
    monkeypatch.setattr(bridge, "agent_system", lambda: None)
    ok, rec = engine.execute("78", "x")
    assert ok and fake_redis.db["taskstate:78"]["status"] == "done"


def test_execute_missing_claude_is_recorded(fake_redis, monkeypatch):
    monkeypatch.setattr(agents, "claude_bin", lambda: None)
    monkeypatch.setattr(bridge, "agent_system", lambda: None)
    ok, rec = engine.execute("79", "x")
    assert not ok and "claude CLI not found" in rec["error"]


def test_listener_moves_files_by_outcome(fake_redis, monkeypatch):
    engine.INCOMING.mkdir()
    (engine.INCOMING / "a.txt").write_text("good task", encoding="utf-8")
    (engine.INCOMING / "b.txt").write_text("   ", encoding="utf-8")
    (engine.INCOMING / "c.txt").write_bytes("hi".encode("utf-16"))
    seen = []
    monkeypatch.setattr(engine, "execute", lambda tid, m: (seen.append(m) or True, {"attempts": 1, "cost_usd": 0}))
    for p in sorted(engine.INCOMING.glob("*.txt")):
        engine._process(p, engine.redis())
    assert seen == ["good task", "hi"]
    assert len(list((engine.INCOMING / "processed").glob("*_a.txt"))) == 1
    assert len(list((engine.INCOMING / "failed").glob("*_b.txt"))) == 1  # blank task is parked, not run


def test_listener_reuses_enqueued_id(fake_redis, monkeypatch):
    tid = engine.enqueue("queued by an agent")
    ids = []
    monkeypatch.setattr(engine, "execute", lambda t, m: (ids.append(t) or True, {"attempts": 1, "cost_usd": 0}))
    engine._process(engine.INCOMING / f"{tid}.txt", engine.redis())
    assert ids == [tid]


def test_single_instance_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    first = engine._single_instance()
    assert first and engine._single_instance() is None and engine.listener_running()
    first.close()
    assert not engine.listener_running()


def test_gate_skips_missing_tools_and_catches_syntax(tmp_path, monkeypatch):
    run = lambda *c: subprocess.run(c, cwd=tmp_path, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i")
    monkeypatch.setattr(gate.shutil, "which", lambda n: None)
    assert gate.run_gates(tmp_path)["ok"]
    (tmp_path / "bad.py").write_text("def x(:\n")
    res = gate.run_gates(tmp_path)
    assert not res["ok"] and res["gates"]["syntax"]["files"] == ["bad.py"]


def test_cli_message_and_enqueue(fake_redis, capsys, monkeypatch):
    monkeypatch.delenv("FLOW_EXEC", raising=False)
    assert cli.main(["task: hello"]) == 0
    assert "Task stored in Redis with ID" in capsys.readouterr().out
    assert cli.main(["enqueue", "later"]) == 0
    tid = capsys.readouterr().out.strip()
    assert cli.main(["status", tid]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert cli.main([]) == 1


def test_cli_redis_down_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.setenv("REDIS_PORT", "1")
    assert cli.main(["x"]) == 1
    assert "Failed to reach Redis" in capsys.readouterr().err


def test_recall_drops_insights_that_name_another_repo(monkeypatch, tmp_path):
    from flow_engine import bridge
    assert bridge.local("note about " + str(tmp_path) + "/a.py", tmp_path)
    assert not bridge.local("[FILE] /mnt/second_drive/Light-Story/src/index.ts", tmp_path)
    assert bridge.local("no paths here", tmp_path)


def test_listener_keeps_a_task_queued_while_redis_is_down(fake_redis, monkeypatch):
    from flow_engine.resp import RedisError
    engine.INCOMING.mkdir()
    path = engine.INCOMING / "a.txt"
    path.write_text("a task", encoding="utf-8")

    def down(message):
        raise RedisError("connection refused")
    monkeypatch.setattr(engine, "submit", down)
    monkeypatch.setattr(engine, "execute", lambda *a: pytest.fail("a task that was never stored must not run"))
    engine._process(path, engine.redis())
    assert path.exists() and not list((engine.INCOMING / "failed").glob("*"))  # still queued, retried on the next poll


def test_enqueued_task_follows_the_callers_model_policy(fake_redis, capsys, monkeypatch):
    for k in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL", "FLOW_REQUIRE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    chains = []
    monkeypatch.setattr(agents, "call", lambda chain, prompt, o, trace=None: (chains.append(chain), (agents.Result(True, "ok", backend=chain[0]), 0.0, {}))[1])
    monkeypatch.setattr(bridge, "recall", lambda m: "")
    monkeypatch.setattr(bridge, "learn", lambda *a: None)
    assert cli.main(["enqueue", "do it", "--model", "gemini-3.8-flash-high"]) == 0
    tid = capsys.readouterr().out.strip()
    assert (engine.INCOMING / f"{tid}.txt").read_text(encoding="utf-8") == "do it"
    engine.execute(tid, "do it")
    engine.execute(engine.submit("no model given"), "no model given")
    assert chains == [[f"agy:{agents.GPT_OSS}!"], ["claude:haiku"]]  # Gemini parent: the hands-on work is pinned to GPT-OSS
    assert engine.status(tid)["model"] == f"agy:{agents.GPT_OSS}!"


def test_require_model_refuses_a_caller_that_does_not_name_its_model(fake_redis, capsys, monkeypatch):
    for k in ("FLOW_BACKENDS", "FLOW_PARENT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FLOW_REQUIRE_MODEL", "1")
    monkeypatch.setattr(cli.plan, "create", lambda *a: pytest.fail("a plan was drafted without a model"))
    assert cli.main(["enqueue", "do it"]) == 1 and cli.main(["plan", "a goal"]) == 1
    assert "--model" in capsys.readouterr().err and not list(engine.INCOMING.glob("*.txt"))
    assert cli.main(["enqueue", "do it", "--model", "gemini-3.8-flash-high"]) == 0
