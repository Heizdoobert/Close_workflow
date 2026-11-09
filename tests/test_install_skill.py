"""install.py copies the task:[...] skill to ~/.claude/skills, only when Claude Code's dir exists, keeping a different old copy."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "closed-loop-task-engine" / "SKILL.md"

spec = importlib.util.spec_from_file_location("flow_install", REPO / "install.py")
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)


def test_skill_ships_in_the_repo_and_has_no_machine_path():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\nname: closed-loop-task-engine\n")
    assert "/mnt/" not in text and "task:[" in text


def test_skill_is_copied_when_claude_dir_exists(tmp_path):
    (tmp_path / ".claude").mkdir()
    dest = install.install_skill(tmp_path)
    assert dest == tmp_path / ".claude" / "skills" / "closed-loop-task-engine" / "SKILL.md"
    assert dest.read_bytes() == SKILL.read_bytes()
    assert not dest.with_name("SKILL.md.bak").exists()
    install.install_skill(tmp_path)  # same content again: no backup, no error
    assert not dest.with_name("SKILL.md.bak").exists()


def test_no_claude_dir_means_nothing_is_created(tmp_path):
    assert install.install_skill(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_a_different_existing_skill_is_backed_up_not_lost(tmp_path):
    dest = tmp_path / ".claude" / "skills" / "closed-loop-task-engine" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("my own edits", encoding="utf-8")
    install.install_skill(tmp_path)
    assert dest.read_bytes() == SKILL.read_bytes()
    assert dest.with_name("SKILL.md.bak").read_text(encoding="utf-8") == "my own edits"


# --- --full: rules blocks, Claude Code MCP, Windows PATH -------------------------------------------------------------

import sys
import types


def _fake_env(monkeypatch, tmp_path, claude_listing, claude_on_path=True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "agent-system"
    exe.write_text("")
    calls = []

    def fake_run(cmd, **kw):
        calls.append([str(c) for c in cmd])
        return types.SimpleNamespace(returncode=0, stdout=claude_listing if cmd[1:3] == ["mcp", "list"] else "")

    monkeypatch.setattr(install, "BIN", bin_dir)
    monkeypatch.setattr(install, "WIN", False)
    monkeypatch.setattr(install, "run", fake_run)
    monkeypatch.setattr(install.shutil, "which", lambda n: "/usr/bin/claude" if claude_on_path and n == "claude" else None)
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: tmp_path))
    return exe, calls


def test_without_full_nothing_is_written(monkeypatch, tmp_path):
    _, calls = _fake_env(monkeypatch, tmp_path, "")
    install.register_rules_and_claude_code(False)
    assert calls == []


def test_full_writes_rules_and_registers_claude_code_once(monkeypatch, tmp_path):
    exe, calls = _fake_env(monkeypatch, tmp_path, "no servers")
    install.register_rules_and_claude_code(True)
    assert [str(exe), "install", "--target", "rules"] in calls
    assert ["/usr/bin/claude", "mcp", "add", "--scope", "user", "agent-system", "--", str(exe), "mcp"] in calls


def test_full_does_not_register_claude_code_twice(monkeypatch, tmp_path):
    _, calls = _fake_env(monkeypatch, tmp_path, "agent-system: connected")
    install.register_rules_and_claude_code(True)
    assert not any(c[1:3] == ["mcp", "add"] for c in calls)


def test_full_without_claude_cli_still_writes_rules(monkeypatch, tmp_path):
    exe, calls = _fake_env(monkeypatch, tmp_path, "", claude_on_path=False)
    install.register_rules_and_claude_code(True)
    assert [str(exe), "install", "--target", "rules"] in calls
    assert not any("mcp" in c for c in calls)


class _Key:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_winreg(monkeypatch, value):
    """A stand-in for the Windows registry (the real module only exists on Windows): HKCU\\Environment\\Path holds `value`, None = unset."""
    store = {"Path": value}
    reg = types.ModuleType("winreg")
    reg.HKEY_CURRENT_USER, reg.KEY_READ, reg.KEY_WRITE, reg.REG_EXPAND_SZ = 1, 2, 4, 2
    reg.OpenKey = lambda *a: _Key()

    def query(key, name):
        if store[name] is None:
            raise FileNotFoundError
        return store[name], 2

    reg.QueryValueEx = query
    reg.SetValueEx = lambda key, name, _r, kind, val: store.__setitem__(name, val)
    monkeypatch.setitem(sys.modules, "winreg", reg)
    return store


def test_windows_path_is_appended_once(monkeypatch):
    store = _with_winreg(monkeypatch, r"C:\Tools;C:\Python")
    assert install.persist_win_path(r"C:\FLOW\.venv\Scripts")
    assert store["Path"] == r"C:\Tools;C:\Python;C:\FLOW\.venv\Scripts"
    install.persist_win_path(r"c:\flow\.venv\scripts")  # same dir, other case: not added again
    assert store["Path"].count("venv") == 1


def test_windows_path_created_when_user_has_none(monkeypatch):
    store = _with_winreg(monkeypatch, None)
    install.persist_win_path(r"C:\FLOW\.venv\Scripts")
    assert store["Path"] == r"C:\FLOW\.venv\Scripts"


def test_agent_setup_doc_exists_and_names_the_real_commands():
    doc = (REPO / "AGENT_SETUP.md").read_text(encoding="utf-8")
    for needle in ("install.py --full", "flow-task doctor", "agent-system doctor", "agent-system:begin", "claude mcp list", "task:["):
        assert needle in doc, needle
    assert "--full" in (REPO / "install.py").read_text(encoding="utf-8")
