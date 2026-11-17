import shlex
import stat

import pytest

from flow_engine import agents, engine, plan, sandbox, secrets


@pytest.fixture
def env_log(tmp_path, monkeypatch):
    """engine.log goes to a temp file, never the repo's logs/ dir; slots live in tmp_path."""
    p = tmp_path / "logs" / "flow_engine.log"
    monkeypatch.setattr(engine, "LOGS", p.parent)
    monkeypatch.setattr(engine, "LOG", p)
    monkeypatch.setattr(sandbox, "_warned", False)
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path / "slots"))
    return p


def test_agent_env_drops_secret_names(monkeypatch):
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    monkeypatch.setenv("FAKE_SERVICE_TOKEN", "x")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/a.sock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fileMode")
    monkeypatch.delenv("FLOW_AGENT_ENV_KEEP", raising=False)

    env = secrets.agent_env()

    assert "REDIS_PASSWORD" not in env and "FAKE_SERVICE_TOKEN" not in env and "SSH_AUTH_SOCK" not in env
    assert "PATH" in env and "HOME" in env and "GIT_CONFIG_KEY_0" in env and "ANTHROPIC_API_KEY" in env


def test_agent_env_keeps_names_from_flow_agent_env_keep(monkeypatch):
    monkeypatch.setenv("FLOW_AGENT_ENV_KEEP", ' FAKE_SERVICE_TOKEN , ,')
    monkeypatch.setenv("FAKE_SERVICE_TOKEN", "x")

    assert "FAKE_SERVICE_TOKEN" in secrets.agent_env()


def test_agent_env_applies_extra(monkeypatch):
    monkeypatch.delenv("FLOW_AGENT_ENV_KEEP", raising=False)
    assert secrets.agent_env({"NO_SANDBOX": "1"})["NO_SANDBOX"] == "1"


def test_claude_subprocess_gets_filtered_env(tmp_path, monkeypatch, env_log):
    bindir = tmp_path / "dir with space"
    bindir.mkdir(parents=True)
    fake = bindir / "fake_claude.sh"
    fake.write_text(
        "#!/bin/sh\n"
        + "env > " + shlex.quote(str(tmp_path / "env.txt")) + "\n"
        + "printf '%s' '{\"type\": \"result\", \"result\": \"ok\", \"usage\": {}}'\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FLOW_CLAUDE", str(fake))
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    monkeypatch.setattr(sandbox, "available", lambda: None)
    wt = tmp_path / "wt"
    wt.mkdir()

    res = agents.run_claude("haiku", "hi", agents.Opts(str(wt), agents.READ_TOOLS))

    assert res.ok
    names = {line.split("=", 1)[0] for line in (tmp_path / "env.txt").read_text().splitlines() if "=" in line}
    assert "REDIS_PASSWORD" not in names and "PATH" in names


def test_acceptance_still_hides_fake_service_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SERVICE_TOKEN", "x")
    r = plan.accept({"acceptance": 'test -z "${FAKE_SERVICE_TOKEN:-}"'}, str(tmp_path))
    assert r["ok"] is True and r["rc"] == 0
