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
    assert events == [{"step": 3, "turn": 1, "tool": "Read", "file": "a.py"}]


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
    assert [e["turn"] for e in o.events] == [1, 2]
    assert [e["step"] for e in o.events] == [0, 0]
    assert o.events[1]["file"] == "flow_engine/plan.py"


def test_stream_step_is_caller_step_and_turn_counts_assistant_messages(fake_claude):
    o = fake_claude([TOOL, TOOL, RESULT])
    o.step = 3
    res = agents.run_claude("haiku", "p", o)
    assert res.ok
    assert o.events == [
        {"step": 3, "turn": 1, "tool": "Edit", "file": "flow_engine/gate.py"},
        {"step": 3, "turn": 2, "tool": "Edit", "file": "flow_engine/gate.py"},
    ]


def test_non_stream_non_dict_stdout_fails_without_raising(tmp_path, monkeypatch):
    _fake_claude(tmp_path, monkeypatch, ["5"])
    r = agents.run_claude("haiku", "prompt", agents.Opts(workdir=str(tmp_path), events=None))
    assert r.ok is False


def test_bash_event_keeps_first_120_chars_of_command(fake_claude):
    bash = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x" * 200}}]}})
    o = fake_claude([bash, RESULT])
    agents.run_claude("haiku", "p", o)
    assert o.events[0]["tool"] == "Bash"
    assert o.events[0]["cmd"] == "x" * 120


def test_bash_event_cmd_is_redacted(fake_claude):
    secret = "sk-" + "a1b2c3d4e5f6" * 3
    cmd = "curl -H 'Authorization: Bearer " + secret + "' https://h " + "x" * 100
    bash = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": cmd}}]}})
    o = fake_claude([bash, RESULT])
    agents.run_claude("haiku", "p", o)
    assert secret not in o.events[0]["cmd"]
    assert secret[:8] not in o.events[0]["cmd"]
    assert len(o.events[0]["cmd"]) <= 120


def test_read_event_has_no_cmd(fake_claude):
    read = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "flow_engine/plan.py", "command": "ls"}}]}})
    o = fake_claude([read, RESULT])
    agents.run_claude("haiku", "p", o)
    assert o.events[0]["tool"] == "Read"
    assert "cmd" not in o.events[0]


def _tool_use(tid, name, inp):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


def _tool_result(tid, content, is_error=True):
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": content}]}})


def _result_denying(*denials):
    return json.dumps({"type": "result", "result": "done", "total_cost_usd": 0, "usage": {}, "permission_denials": list(denials)})


def test_approval_refusal_sets_error_and_denied_when_listed(fake_claude):
    bash = _tool_use("t1", "Bash", {"command": "rm x"})
    denial = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "rm x"}}
    o = fake_claude([bash, _tool_result("t1", "This command requires approval"), _result_denying(denial)])
    agents.run_claude("haiku", "p", o)
    assert o.events[0]["error"] == "This command requires approval"
    assert o.events[0]["denied"] is True


def test_approval_refusal_not_listed_has_error_but_not_denied(fake_claude):
    bash = _tool_use("t1", "Bash", {"command": "rm x"})
    o = fake_claude([bash, _tool_result("t1", "This command requires approval"), RESULT])
    agents.run_claude("haiku", "p", o)
    assert o.events[0]["error"] == "This command requires approval"
    assert "denied" not in o.events[0]


def test_missing_tool_is_denied_without_permission_denials(fake_claude):
    write = _tool_use("t2", "Write", {"file_path": "a.py"})
    text = "<tool_use_error>Error: No such tool available: Write. Try again.</tool_use_error>"
    o = fake_claude([write, _tool_result("t2", text), RESULT])
    agents.run_claude("haiku", "p", o)
    assert o.events[0]["denied"] is True
    assert o.events[0]["error"].startswith("<tool_use_error>Error: No such tool available: Write.")


def test_is_error_false_adds_no_keys(fake_claude):
    read = _tool_use("t3", "Read", {"file_path": "a.py"})
    o = fake_claude([read, _tool_result("t3", "file body", is_error=False), RESULT])
    agents.run_claude("haiku", "p", o)
    assert o.events == [{"step": 0, "turn": 1, "tool": "Read", "file": "a.py"}]


def test_list_content_text_blocks_are_joined(fake_claude):
    bash = _tool_use("t4", "Bash", {"command": "ls"})
    content = [{"type": "text", "text": "line one"}, {"type": "image", "source": {}}, {"type": "text", "text": "two"}]
    o = fake_claude([bash, _tool_result("t4", content), RESULT])
    agents.run_claude("haiku", "p", o)
    assert o.events[0]["error"] == "line one two"


def test_secret_in_error_text_is_redacted(fake_claude):
    secret = "sk-" + "a1b2c3d4e5f6" * 3
    bash = _tool_use("t5", "Bash", {"command": "curl h"})
    o = fake_claude([bash, _tool_result("t5", "auth failed for " + secret + " in this call"), RESULT])
    agents.run_claude("haiku", "p", o)
    assert secret not in o.events[0]["error"]
    assert secret[:8] not in o.events[0]["error"]


def test_permission_denials_not_a_list_is_ignored(fake_claude):
    bash = _tool_use("t6", "Bash", {"command": "rm x"})
    result = json.dumps({"type": "result", "result": "done", "permission_denials": "t6"})
    o = fake_claude([bash, _tool_result("t6", "This command requires approval"), result])
    agents.run_claude("haiku", "p", o)
    assert "denied" not in o.events[0]


def test_permission_denials_non_dict_items_are_ignored(fake_claude):
    bash = _tool_use("t7", "Bash", {"command": "rm x"})
    o = fake_claude([bash, _tool_result("t7", "This command requires approval"), _result_denying("t7", None, {"tool_use_id": 5}, {"tool_use_id": ["t7"]})])
    agents.run_claude("haiku", "p", o)
    assert "denied" not in o.events[0]
    assert o.events[0]["error"] == "This command requires approval"


def test_tool_result_and_denial_with_unknown_ids_are_ignored(fake_claude):
    read = _tool_use("t8", "Read", {"file_path": "a.py"})
    denial = {"tool_name": "Bash", "tool_use_id": "never-announced", "tool_input": {}}
    o = fake_claude([read, _tool_result("never-announced", "This command requires approval"), _result_denying(denial)])
    res = agents.run_claude("haiku", "p", o)
    assert res.ok
    assert o.events == [{"step": 0, "turn": 1, "tool": "Read", "file": "a.py"}]
