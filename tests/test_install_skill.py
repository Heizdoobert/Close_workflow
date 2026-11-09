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
