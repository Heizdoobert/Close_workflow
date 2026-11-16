import json
import sys

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
    assert events == [{"step": 3, "tool": "Read", "file": "a.py"}]


def test_json_path_without_events_still_returns_text(tmp_path, monkeypatch):
    line = json.dumps({"type": "result", "result": "ok", "usage": {}, "total_cost_usd": 0, "is_error": False})
    _fake_claude(tmp_path, monkeypatch, [line])
    r = agents.run_claude("haiku", "prompt", agents.Opts(workdir=str(tmp_path), events=None))
    assert r.ok is True
    assert r.text == "ok"
