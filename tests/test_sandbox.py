import json
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from flow_engine import agents, engine, sandbox

HERE = Path(__file__).parent
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries and bwrap are POSIX only")


def launcher(d, script):
    w = Path(d) / (Path(script).stem + ".sh")
    w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    return str(w)


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FLOW_CLAUDE", launcher(tmp_path, str(HERE / "fake_claude.py")))
    monkeypatch.setenv("FAKE_PLAN", "/dev/null")
    monkeypatch.setenv("FAKE_STEPS", str(tmp_path / "steps.json"))
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.delenv("FLOW_BACKENDS", raising=False)
    (tmp_path / "steps.json").write_text("{}")
    return tmp_path


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """engine.log goes to a temp file, never the repo's logs/ dir."""
    p = tmp_path / "logs" / "flow_engine.log"
    monkeypatch.setattr(engine, "LOGS", p.parent)
    monkeypatch.setattr(engine, "LOG", p)
    monkeypatch.setattr(sandbox, "_warned", False)
    return p


def pair(args, flag, path):
    """True when args contains `flag path path` (bwrap binds take the same path twice)."""
    return any(args[i:i + 3] == [flag, path, path] for i in range(len(args) - 2))


def test_wrap_binds_worktree_writable_root_read_only_and_not_plans(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    plans = tmp_path / "repo" / "plans" / "p1"
    wt = plans / "wt" / "s1"
    wt.mkdir(parents=True)

    cmd = sandbox.wrap(["claude", "-p"], str(wt))

    assert cmd[0] == "bwrap"
    assert pair(cmd, "--bind", str(wt.resolve()))  # the worktree is the writable project path
    assert cmd[cmd.index("--ro-bind") + 1:cmd.index("--ro-bind") + 3] == ["/", "/"]  # everything else read-only
    assert pair(cmd, "--bind", "/tmp")
    assert pair(cmd, "--bind", str(home / ".claude")) and pair(cmd, "--bind", str(home / ".claude.json"))
    assert not any(cmd[i] == "--bind" and cmd[i + 1] in (str(plans), str(plans.parent), str(plans.parent.parent)) for i in range(len(cmd) - 1))
    assert "--die-with-parent" in cmd
    assert cmd[cmd.index("--") + 1:] == ["claude", "-p"]


def test_wrap_skips_claude_config_that_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    wt = tmp_path / "wt"
    wt.mkdir()
    cmd = sandbox.wrap(["x"], str(wt))
    assert [cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i] == "--bind"] == ["/tmp", str(wt.resolve())]


def test_run_claude_wraps_command_when_bwrap_available(fakes, monkeypatch):
    seen = {}

    def fake_run(cmd, o, stdin=None, env=None):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"type": "result", "result": "ok", "usage": {}}), "")

    monkeypatch.setattr(sandbox, "available", lambda: "bwrap")
    monkeypatch.setattr(agents, "_run", fake_run)
    wt = fakes / "wt"
    wt.mkdir()
    res = agents.run_claude("haiku", "hi", agents.Opts(str(wt), agents.WRITE_TOOLS))
    assert res.ok
    assert seen["cmd"][0] == "bwrap"
    assert seen["cmd"][seen["cmd"].index("--") + 1] == agents.claude_bin()  # the real command sits behind the sandbox flags


def test_no_sandbox_logs_one_warning_and_fake_claude_still_runs(fakes, log_file, monkeypatch):
    monkeypatch.setattr(sandbox, "available", lambda: None)
    wt = fakes / "wt"
    wt.mkdir()
    res, _, _ = agents.call(["claude:haiku"], "planning agent hello", agents.Opts(str(wt)), [])
    res2, _, _ = agents.call(["claude:haiku"], "planning agent hello", agents.Opts(str(wt)), [])

    assert res.ok and res2.ok
    lines = [json.loads(l) for l in log_file.read_text().splitlines()]
    warnings = [l for l in lines if l["event"] == "sandbox"]
    assert len(warnings) == 1
    assert "escaping_paths" in warnings[0]["message"]
    assert len([c for c in (fakes / "calls.log").read_text().splitlines() if c.startswith("claude")]) == 2


def test_real_bwrap_allows_worktree_write_and_blocks_parent_write(tmp_path):
    if sandbox.available() != "bwrap":
        # no working bwrap here: the no-sandbox path above covers this host
        assert sandbox.available() is None
        return
    # /tmp is writable inside the sandbox by design, so the probe tree lives in /var/tmp to test the plans dir
    root = Path(tempfile.mkdtemp(prefix="flow-sandbox-", dir="/var/tmp"))
    try:
        wt = root / "plans" / "p1" / "wt" / "s1"
        wt.mkdir(parents=True)
        inside, outside = wt / "inside.txt", root / "plans" / "outside.txt"
        script = f"echo ok > {inside} && echo bad > {outside}"  # the second write must fail
        p = subprocess.run(sandbox.wrap(["sh", "-c", script], str(wt)), cwd=str(wt), capture_output=True, text=True)

        assert inside.read_text().strip() == "ok"
        assert not outside.exists()
        assert p.returncode != 0

        ok = subprocess.run(sandbox.wrap(["sh", "-c", f"echo ok > {inside}.2"], str(wt)), cwd=str(wt), capture_output=True, text=True)
        assert ok.returncode == 0 and Path(f"{inside}.2").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_wrap_hides_secret_dir_and_engine_env_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("TOKEN=abc")
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(engine, "ROOT", root)

    cmd = sandbox.wrap(["x"], str(wt))

    assert any(cmd[i:i + 2] == ["--tmpfs", str(home / ".ssh")] for i in range(len(cmd) - 1))
    assert any(cmd[i:i + 3] == ["--ro-bind", "/dev/null", str(root / ".env")] for i in range(len(cmd) - 2))


def test_wrap_skips_secret_paths_that_do_not_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    root = tmp_path / "empty-root"
    root.mkdir()
    monkeypatch.setattr(engine, "ROOT", root)
    wt = tmp_path / "wt"
    wt.mkdir()

    cmd = sandbox.wrap(["x"], str(wt))

    assert "--tmpfs" not in cmd and "/dev/null" not in cmd


@pytest.mark.parametrize("rel", sandbox.SECRET_DIRS + sandbox.SECRET_FILES)
def test_wrap_hides_each_listed_secret_path(rel, tmp_path, monkeypatch):
    home = tmp_path / "home"
    path = home / rel
    if rel in sandbox.SECRET_DIRS:
        path.mkdir(parents=True)
        want = ["--tmpfs", str(path)]
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret")
        want = ["--ro-bind", "/dev/null", str(path)]
    root = tmp_path / "root"
    root.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(engine, "ROOT", root)

    cmd = sandbox.wrap(["x"], str(wt))

    assert any(cmd[i:i + len(want)] == want for i in range(len(cmd)))


def test_wrap_does_not_hide_secret_paths_inside_the_worktree(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    (wt / ".ssh").mkdir(parents=True)
    (wt / ".env").write_text("TOKEN=abc")
    monkeypatch.setenv("HOME", str(wt))
    monkeypatch.setattr(engine, "ROOT", wt)

    cmd = sandbox.wrap(["x"], str(wt))

    assert str(wt / ".ssh") not in cmd and str(wt / ".env") not in cmd


def test_real_bwrap_hides_engine_env_file(monkeypatch):
    if sandbox.available() != "bwrap":
        # no working bwrap here: the no-sandbox path above covers this host
        assert sandbox.available() is None
        return
    # /tmp is writable inside the sandbox by design, so the probe tree lives in /var/tmp
    root = Path(tempfile.mkdtemp(prefix="flow-sandbox-", dir="/var/tmp"))
    try:
        wt = root / "plans" / "p1" / "wt" / "s1"
        wt.mkdir(parents=True)
        (root / ".env").write_text("VISIBLE_MARK=1")
        monkeypatch.setattr(engine, "ROOT", root)

        # bwrap mounts binds nodev, so /dev/null over the file reads as "Permission denied" (another bwrap may read it as empty)
        p = subprocess.run(sandbox.wrap(["sh", "-c", 'echo started; cat "$1"', "sh", str(root / ".env")], str(wt)), cwd=str(wt), capture_output=True, text=True)

        assert "started" in p.stdout, p.stderr  # the sandbox really ran: a bwrap that failed to start prints nothing either
        assert "VISIBLE_MARK" not in p.stdout
        assert "VISIBLE_MARK" in (root / ".env").read_text()
    finally:
        shutil.rmtree(root, ignore_errors=True)
