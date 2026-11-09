"""Per-step journal, reflection before a fix, loop detection, drift warning and `flow-task where`. Uses the fake agents from test_plan."""

import json
import sys

import pytest

from flow_engine import cli, journal, plan
from test_plan import env, step  # noqa: F401  (env is the shared fake-agent fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
FAKE_KEY = "sk-ant-FAKE1234567890ABCDEF"
REFL = "REFLECTION: acceptance failed, the file said BAD, I write GOOD"


@pytest.fixture
def prompts(monkeypatch):
    """Record every prompt a step agent is given; a reply to a retry opens with a REFLECTION line, as a real agent would."""
    seen = []
    real = plan.agents.call

    def call(chain, prompt, opts, trace):
        res, cost, tok = real(chain, prompt, opts, trace)
        if "Your step [" in prompt:
            seen.append(prompt)
            if "did not pass" in prompt:
                res.text = f"{REFL}\n{res.text}"
        return res, cost, tok
    monkeypatch.setattr(plan.agents, "call", call)
    return seen


def _state(env, pid):
    return json.loads((env.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))


def test_journal_keeps_every_attempt_redacted_and_brief_respects_the_limit(env):
    pid, sid = "p-journal", "s1"
    for n in range(1, 31):
        journal.append(pid, sid, {"n": n, "status": "acceptance-failed", "failed": ["acceptance"], "files": ["a.py"],
                                  "reflection": f"REFLECTION: try {n} with {FAKE_KEY} é" + " x" * 40})
    rows = journal.read(pid, sid)
    assert len(rows) == 30 and rows[-1]["n"] == 30
    raw = (env.home / "plans" / pid / "journal" / "s1.jsonl").read_text(encoding="utf-8")
    assert FAKE_KEY not in raw and "[REDACTED]" in raw
    short = journal.brief(pid, sid, limit=400)
    assert len(short.encode("utf-8")) <= 400
    lines = short.splitlines()
    assert lines and all(x.startswith("attempt ") for x in lines) and lines[-1].startswith("attempt 30 ")  # whole lines, newest kept
    assert len(journal.brief(pid, sid).encode("utf-8")) > len(short.encode("utf-8"))


def test_failed_attempt_is_journaled_and_the_retry_asks_for_a_reflection_first(env, prompts):
    env.script([step("s1", files=["notes.txt"], acc="grep -q GOOD notes.txt")],
               {"s1": {"files": {"notes.txt": "BAD\n"}, "fixed_files": {"notes.txt": "GOOD\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    rows = journal.read(pid, "s1")
    assert [r["status"] for r in rows] == ["acceptance-failed", "passed"]
    assert rows[0]["failed"] == ["acceptance"] and rows[0]["files"] == ["notes.txt"]
    assert rows[1]["reflection"] == REFL
    retry = prompts[1]
    assert "REFLECTION:" in retry and "did not pass the automated checks" in retry
    assert "rc=1" in retry  # the first failure is fed back
    st = _state(env, pid)["steps"]["s1"]
    assert st["reflection"] == REFL and "stuck" not in st


def test_resumed_step_is_told_its_earlier_attempts(env, prompts, monkeypatch):
    monkeypatch.setenv("FLOW_RETRIES", "0")
    env.script([step("s1", files=["notes.txt"], acc="grep -q GOOD notes.txt")], {"s1": {"files": {"notes.txt": "BAD\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "failed"
    env.script([step("s1", files=["notes.txt"], acc="grep -q GOOD notes.txt")], {"s1": {"files": {"notes.txt": "GOOD\n"}}})
    d = env.home / "plans" / pid
    p = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    ctx = plan.Ctx(pid, d, p, _state(env, pid))
    ctx.reserved["s1"] = ctx.step_usd  # what run() reserves before it launches a step
    assert plan.do_step(ctx, p["steps"][0]) is True
    resumed = prompts[-1]
    assert plan.EARLIER in resumed and "attempt 1 acceptance-failed" in resumed
    assert resumed.index(plan.EARLIER) < resumed.index("Your step [s1]")
    assert [r["status"] for r in journal.read(pid, "s1")] == ["acceptance-failed", "passed"]


def test_the_same_failure_three_times_ends_stuck_without_more_attempts(env, monkeypatch):
    monkeypatch.setenv("FLOW_RETRIES", "5")
    env.script([step("s1", files=["notes.txt"], acc="grep -q GOOD notes.txt")], {"s1": {"files": {"notes.txt": "BAD\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "failed"
    st = _state(env, pid)["steps"]["s1"]
    assert st["stuck"] is True and st["error"] == "stuck: identical failure repeated 3 times"
    assert len(st["attempts"]) == 3
    assert len([c for c in env.calls() if c[0] == "step-start" and c[1] == "s1"]) == 3  # the 4th and 5th attempts never ran
    assert journal.read(pid, "s1")[-1]["status"] == "stuck"
    assert "stuck" in (env.home / "plans" / pid / "report.md").read_text(encoding="utf-8")


def test_a_file_outside_the_plan_is_drift_but_its_test_file_is_not(env):
    env.script([step("s1", files=["a.py"], acc="test -f a.py")],
               {"s1": {"files": {"a.py": "1\n", "extra.txt": "x\n", "tests/test_a.py": "def test_x():\n    assert 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"  # drift never blocks the merge
    assert _state(env, pid)["steps"]["s1"]["drift"] == ["extra.txt"]
    assert [r["drift"] for r in journal.read(pid, "s1") if r["status"] == "drift"] == [["extra.txt"]]
    assert "drift: extra.txt" in (env.home / "plans" / pid / "report.md").read_text(encoding="utf-8")


def test_where_from_another_folder_names_the_next_action_and_writes_status_md(env, monkeypatch, tmp_path, capsys):
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "1\n"}}})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # FLOW_WORKDIR (set by the fixture) names the plan's repo
    pid = plan.create("g" * 300)
    assert cli.main(["where"]) == 0
    out = capsys.readouterr().out
    assert f"Plan {pid}: awaiting_approval  branch flow/{pid}" in out and f"Next: flow-task approve {pid}" in out
    assert len(out.encode("utf-8")) < 1500 and "g" * 201 not in out  # the goal is cut to 200 chars
    assert plan.approve(pid, wait_for=True) == "done"
    assert cli.main(["where"]) == 0
    out = capsys.readouterr().out
    assert "s1 done 1" in out and "Next: review flow/" in out and "report.md" in out
    assert len(out.encode("utf-8")) < 1500
    assert (env.home / "plans" / pid / "STATUS.md").read_text(encoding="utf-8") + "\n" == out  # same text in the file


def test_where_refuses_when_this_repo_has_no_plan(env, monkeypatch, capsys):
    monkeypatch.delenv("FLOW_WORKDIR")
    monkeypatch.chdir(env.repo)  # a git repo, but no plan has been made in it
    assert cli.main(["where"]) == 1
    assert "no plan for this repo" in capsys.readouterr().err


def test_where_text_stays_under_the_limit_for_a_big_plan(tmp_path):
    steps = [step(f"s{i}") for i in range(8)]
    plan_ = {"goal": "x" * 5000, "steps": steps}
    state = {"status": "failed", "steps": {s["id"]: {"status": "failed", "attempts": [{"n": 1, "agent_error": "é" * 300}], "error": "x"} for s in steps}}
    out = plan.where_text("p1", plan_, state, tmp_path)
    assert len(out.encode("utf-8")) <= plan.WHERE_LIMIT and out.splitlines()[-1].startswith("Next: flow-task retry p1")
    cut = plan.where_text("p1", plan_, state, tmp_path, limit=300)
    assert len(cut.encode("utf-8")) <= 300 and "..." in cut and cut.splitlines()[-1].startswith("Next: flow-task retry p1")
