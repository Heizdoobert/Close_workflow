"""Gate holes found by the war-room review: a repo-side allowlist, a scan that ran before the acceptance command, and gates that passed when git failed."""
import shutil
import subprocess
import sys

import pytest

from flow_engine import gate

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell and git fixtures")


def git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.py").write_text("A = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def token():
    return "".join(["gh", "p_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"])  # built at runtime: no key-shaped literal in this file


def test_gitleaks_runs_with_a_pinned_config_and_without_allow_comments(repo, monkeypatch):
    real_which, real_run, seen = shutil.which, gate._run, {}
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: "/bin/gitleaks" if name == "gitleaks" else real_which(name, *a, **k))

    def fake_run(root, cmd, **kw):
        if cmd[0] == "gitleaks":
            seen["cmd"] = cmd
            seen["config"] = open(cmd[cmd.index("-c") + 1], encoding="utf-8").read()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(root, cmd, **kw)
    monkeypatch.setattr(gate, "_run", fake_run)
    assert gate.run_gates(str(repo))["gates"]["secrets"]["ok"]
    assert "--ignore-gitleaks-allow" in seen["cmd"] and "useDefault = true" in seen["config"]


@pytest.mark.skipif(not shutil.which("gitleaks"), reason="needs the gitleaks binary")
def test_a_repo_allowlist_and_an_allow_comment_do_not_hide_a_key(repo):
    (repo / ".gitleaks.toml").write_text("[allowlist]\npaths = ['''leak\\.txt''']\n")
    (repo / "leak.txt").write_text(f"KEY = {token()}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "x")
    assert gate.run_gates(str(repo))["gates"]["secrets"]["ok"] is False
    (repo / ".gitleaks.toml").unlink()
    (repo / "leak.txt").write_text(f"KEY = {token()}  # gitleaks:" + "allow\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "y")
    assert gate.run_gates(str(repo))["gates"]["secrets"]["ok"] is False


def test_adding_an_allow_comment_or_scanner_config_is_a_weakened_bar(repo):
    (repo / "b.py").write_text("B = 1  # gitleaks:" + "allow\n")
    r = gate.constraints(str(repo))
    assert r["ok"] is False and r["weakened"] == 1
    (repo / "b.py").write_text("B = 1\n")
    (repo / ".gitleaks.toml").write_text("[allowlist]\n")
    r = gate.constraints(str(repo))
    assert r["ok"] is False and any("secret scanner config" in f for f in r["findings"])


def test_gates_fail_when_git_cannot_show_the_change(tmp_path):
    out = gate.run_gates(str(tmp_path))  # not a repository: nothing could be checked
    assert out["ok"] is False and "git" in out["gates"] and not out["gates"]["git"]["ok"]


def test_gates_fail_when_head_is_unreadable(repo):
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/gone\n")
    (repo / "a.py").write_text("")  # a gutted file that a blind gate would let through
    out = gate.run_gates(str(repo))
    assert out["ok"] is False and "git" in out["gates"]
