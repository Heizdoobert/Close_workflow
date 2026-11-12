"""Secrets gate fails closed: without gitleaks the built-in regex scan runs over the step's added lines and untracked files."""

import shutil
import subprocess

import pytest

from flow_engine import agents, cli, engine, gate, secrets

REAL_WHICH = shutil.which
# built at runtime with join (not +, which CPython folds into one constant in the .pyc) so neither this source nor its bytecode holds a key-shaped literal
CANARY_SK = "".join(["sk", "-ant", "-", "Q1w2E3r4T5y6U7i8O9p0"])
CANARY_AWS = "".join(["AKIA", "ABCDEFGHIJKLMNOP"])


def git(cwd, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("hello\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    # gitleaks is hidden on purpose: the test must not depend on whether it is installed
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "gitleaks" else REAL_WHICH(name, *a, **k))
    return root


def test_redact_shapes_masks_key_formats_but_leaves_ordinary_code():
    code = f'token = self.next_token()\npassword = get_password()\nkey = "{CANARY_AWS}"\nother = "{CANARY_SK}"\n'
    out = secrets.redact_shapes(code)
    assert CANARY_AWS not in out and CANARY_SK not in out and out.count("[REDACTED]") == 2
    assert "token = self.next_token()" in out and "password = get_password()" in out
    assert "[REDACTED]" in secrets.redact("token = self.next_token()")  # why a reviewer's diff gets the shapes only


def test_planted_key_in_untracked_file_fails_the_secrets_gate(repo):
    (repo / "config.py").write_text(f"API = '{CANARY_SK}'\n")
    res = gate.run_gates(str(repo))
    sec = res["gates"]["secrets"]
    assert sec["ok"] is False
    assert sec["engine"] == "builtin"
    assert "config.py:1 sk-key" in sec["findings"]
    assert res["ok"] is False


def test_key_in_added_line_of_a_tracked_file_fails_and_is_not_echoed(repo):
    (repo / "README.md").write_text(f"hello\n{CANARY_AWS}\n")
    sec = gate.run_gates(str(repo))["gates"]["secrets"]
    assert sec["ok"] is False
    assert "README.md:2 aws-key" in sec["findings"]
    assert all("AKIA" not in f for f in sec["findings"])


def test_planted_env_file_fails_by_name(repo):
    (repo / ".env").write_text("DEBUG=1\n")
    sec = gate.run_gates(str(repo))["gates"]["secrets"]
    assert sec["ok"] is False
    assert ".env secret-file-name" in sec["findings"]


def test_env_example_is_not_a_secret_file(repo):
    (repo / ".env.example").write_text("DEBUG=1\n")
    sec = gate.run_gates(str(repo))["gates"]["secrets"]
    assert sec == {"ok": True, "engine": "builtin", "findings": []}


def test_clean_file_passes_with_builtin_engine(repo):
    (repo / "app.py").write_text("print('hello')\n")
    sec = gate.run_gates(str(repo))["gates"]["secrets"]
    assert sec == {"ok": True, "engine": "builtin", "findings": []}


def test_binary_file_is_skipped_by_the_scan(repo):
    (repo / "blob.bin").write_bytes(b"\0\1" + CANARY_SK.encode())
    sec = gate.secrets_builtin(str(repo))
    assert sec["ok"] is True


def test_doctor_reports_missing_gitleaks_as_a_warning_that_does_not_fail(monkeypatch, repo):
    monkeypatch.setenv("REDIS_PASSWORD", "x")
    monkeypatch.setattr(engine, "redis", lambda: type("R", (), {"call": lambda self, *a: "PONG"})())
    monkeypatch.setattr(engine, "listener_running", lambda: False)
    monkeypatch.setattr(agents, "claude_bin", lambda: "/bin/true")
    monkeypatch.setattr(cli.sandbox, "available", lambda: None)
    monkeypatch.setattr(cli.bridge, "agent_system", lambda: None)
    checks = {name: (ok, detail, req) for name, ok, detail, req in cli.doctor_checks()}
    ok, detail, req = checks["gitleaks"]
    assert ok is False and req is False
    assert detail.startswith("missing: secrets gate uses the built-in regex scan only")
    assert cli.doctor() == 0


def test_secret_redaction_still_reachable_from_plan_module():
    from flow_engine import plan

    assert plan.redact is secrets.redact
    assert plan.SECRET_PATTERNS is secrets.SECRET_PATTERNS
    assert "[REDACTED]" in plan.redact("token=" + "abcdefghijklmnop")


def test_redacting_state_never_breaks_the_json():
    """A scout quote such as `f"Unexpected token: {tok}"` matches the assigned-secret rule; masking the serialised text ate a closing quote and left state.json unparsable (live bench, 2026-11-12)."""
    import json
    from flow_engine import secrets
    state = {"scout": {"facts": [{"quote": 'raise ValueError(f"Unexpected token: {tok}")', "note": "x"}]}, "k": 'password = "hunter2hunter2"'}
    assert not _parses(secrets.redact(json.dumps(state, indent=2)))  # the old way: the mask swallows the closing quote
    out = json.loads(json.dumps(secrets.redact_obj(state), indent=2))
    assert "hunter2" not in json.dumps(out) and "[REDACTED]" in json.dumps(out)


def _parses(text):
    import json
    try:
        json.loads(text)
        return True
    except ValueError:
        return False
