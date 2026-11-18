"""H0: the test-code environment drops every secret name, and wrap(claude=False) hides claude's config."""

import os

from flow_engine import sandbox, secrets


def test_no_secret_env_drops_secret_names_and_keeps_path(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))

    env = secrets.no_secret_env()

    assert "REDIS_PASSWORD" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env


def test_wrap_claude_false_hides_claude_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}")
    wd = tmp_path / "wd"
    wd.mkdir()

    cmd = sandbox.wrap(["x"], wd, claude=False)

    assert not any(a == "--bind" and i + 1 < len(cmd) and cmd[i + 1] in (str(claude_dir), str(claude_json))
                   for i, a in enumerate(cmd))
    assert any(a == "--tmpfs" and i + 1 < len(cmd) and cmd[i + 1] == str(claude_dir) for i, a in enumerate(cmd))
    assert any(a == "/dev/null" and i + 1 < len(cmd) and cmd[i + 1] == str(claude_json) for i, a in enumerate(cmd))


def test_wrap_default_still_binds_claude_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    wd = tmp_path / "wd"
    wd.mkdir()

    cmd = sandbox.wrap(["x"], wd)

    assert any(a == "--bind" and i + 1 < len(cmd) and cmd[i + 1] == str(claude_dir) for i, a in enumerate(cmd))
