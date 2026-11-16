import json
import shlex
import sys

import pytest

from flow_engine import agents


def _fake_claude(tmp_path, monkeypatch, lines):
    """A fake claude: a sh wrapper that runs a python script printing the given NDJSON lines."""
    script = tmp_path / "fake_claude.py"
    script.write_text("import sys\n" + "".join(f"print({json.dumps(line)})\n" for line in lines), encoding="utf-8")
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("FLOW_CLAUDE", str(wrapper))
    monkeypatch.setattr(agents.sandbox, "available", lambda: None)
    monkeypatch.setattr(agents.sandbox, "warn_no_sandbox", lambda: None)


def test_stream_json_records_tool_events(tmp_path, monkeypatch):
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}}),
        json.dumps({"type": "result", "result": "ok", "usage": {"input_tokens": 3, "output_tokens": 2}, "total_cost_usd": 0.01, "is_error": False}),
    ]
    _fake_claude(tmp_path, monkeypatch, lines)
    events = []
    r = agents.run_claude("haiku", "prompt", agents.Opts(workdir=str(tmp_path), events=events, step=3))
    assert r.ok is True
    assert r.text == "ok"
    assert events == [{"step": 4, "tool": "Read", "file": "a.py"}]


def test_json_path_without_events_still_returns_text(tmp_path, monkeypatch):
    line = json.dumps({"type": "result", "result": "ok", "usage": {}, "total_cost_usd": 0, "is_error": False})
    _fake_claude(tmp_path, monkeypatch, [line])
    r = agents.run_claude("haiku", "prompt", agents.Opts(workdir=str(tmp_path), events=None))
    assert r.ok is True
    assert r.text == "ok"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.sandbox, "available", lambda: None)
    monkeypatch.setattr(agents.sandbox, "warn_no_sandbox", lambda: None)

    def make(lines):
        body = tmp_path / "stream.txt"
        body.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        exe = tmp_path / "fake claude.sh"
        exe.write_text("#!/bin/sh" + chr(10) + "cat " + shlex.quote(str(body)) + chr(10), encoding="utf-8")
        exe.chmod(0o755)
        monkeypatch.setenv("FLOW_CLAUDE", str(exe))
        return agents.Opts(workdir=str(tmp_path), events=[])

    return make


RESULT = json.dumps({"type": "result", "result": "done", "total_cost_usd": 0.01, "usage": {"input_tokens": 3, "output_tokens": 2}})
TOOL = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "flow_engine/gate.py"}}]}})


def test_non_object_lines_are_skipped(fake_claude):
    o = fake_claude(["5", "[]", TOOL, RESULT])
    res = agents.run_claude("haiku", "p", o)
    assert res.ok
    assert res.text == "done"
    assert len(o.events) == 1


def test_is_error_true_fails(fake_claude):
    o = fake_claude([json.dumps({"type": "result", "is_error": True, "result": "bad"})])
    res = agents.run_claude("haiku", "p", o)
    assert not res.ok


def test_missing_usage_is_zero(fake_claude):
    o = fake_claude([json.dumps({"type": "result", "result": "done"})])
    res = agents.run_claude("haiku", "p", o)
    assert res.ok
    assert res.tokens["input_tokens"] == 0


def test_no_result_line_fails(fake_claude):
    o = fake_claude(["5", "[]"])
    res = agents.run_claude("haiku", "p", o)
    assert not res.ok


def test_steps_follow_assistant_turns(fake_claude):
    turn2 = json.dumps({"type": "assistant", "message": {"content": ["x", {"type": "tool_use", "name": "Read", "input": {"path": "flow_engine/plan.py"}}]}})
    o = fake_claude([TOOL, turn2, RESULT])
    agents.run_claude("haiku", "p", o)
    assert [e["step"] for e in o.events] == [1, 2]
    assert o.events[1]["file"] == "flow_engine/plan.py"
