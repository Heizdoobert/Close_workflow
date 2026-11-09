"""Scripted quality gates (0 LLM tokens). A missing optional tool means the gate is skipped, never failed.

Gates: secrets (gitleaks), syntax of changed .py/.sh, tests (pytest if tests exist),
constraints (no weakened bar in the git diff), critic (agent-system rules, opt-in FLOW_CRITIC=1).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from flow_engine import bridge


def _run(root, cmd, **kw):
    try:
        return subprocess.run(cmd, cwd=root, capture_output=True, text=True, **kw)
    except OSError as e:  # tool missing
        return subprocess.CompletedProcess(cmd, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout after {kw.get('timeout')}s")


def _changed(root, *exts):
    out = _run(root, ["git", "diff", "--name-only", "HEAD"]).stdout.split()
    out += _run(root, ["git", "ls-files", "-o", "--exclude-standard"]).stdout.split()
    return [f for f in out if f.endswith(exts) and os.path.isfile(os.path.join(root, f))]


def run_gates(root="."):
    root = os.path.abspath(root)
    gates = {}

    if shutil.which("gitleaks"):
        with tempfile.TemporaryDirectory() as td:
            rep = os.path.join(td, "r.json")
            r = _run(root, ["gitleaks", "dir", ".", "--no-banner", "--redact", "-r", rep, "-f", "json"], timeout=120)
            found = json.load(open(rep)) if os.path.exists(rep) and os.path.getsize(rep) else []
            gates["secrets"] = {"ok": r.returncode == 0, "findings": [f"{f['File']}:{f['StartLine']} {f['RuleID']}" for f in found][:10]}
    else:
        gates["secrets"] = {"ok": True, "skipped": "gitleaks not installed"}

    bad = [f for f in _changed(root, ".py") if _run(root, [sys.executable, "-m", "py_compile", f]).returncode]
    if shutil.which("bash"):
        bad += [f for f in _changed(root, ".sh") if _run(root, ["bash", "-n", f]).returncode]
    gates["syntax"] = {"ok": not bad, "files": bad}

    if _run(root, ["git", "ls-files", "test_*.py", "*_test.py", "tests/*"]).stdout.strip():
        r = _run(root, [sys.executable, "-m", "pytest", "-q", "-x", "--no-header"], timeout=600)
        if "No module named pytest" in r.stderr + r.stdout:
            gates["tests"] = {"ok": True, "skipped": "pytest not installed"}
        else:
            gates["tests"] = {"ok": r.returncode == 0, "tail": r.stdout.strip().splitlines()[-1:]}
    else:
        gates["tests"] = {"ok": True, "skipped": "no tests found"}

    diff = _run(root, ["git", "diff", "HEAD", "-U0"]).stdout
    weak = re.compile(r"^\+.*(@ts-ignore|eslint-disable|type:\s*ignore|pytest\.mark\.(skip|xfail)|\bxit\(|\bit\.skip|noqa)", re.M)
    dropped = [l for l in diff.splitlines() if not l.startswith("---") and re.match(r"^-\s*(def test_|it\(|test\()", l)]
    hits = weak.findall(diff)
    gates["constraints"] = {"ok": not hits and not dropped, "weakened": len(hits), "removed_tests": len(dropped)}

    if os.environ.get("FLOW_CRITIC") == "1" and bridge.agent_system():
        fails = [f for f in _changed(root, ".py") if bridge.run_agent("critic", "--file", f, cwd=root).returncode]
        gates["critic"] = {"ok": not fails, "files": fails[:10]}

    return {"ok": all(g["ok"] for g in gates.values()), "gates": gates}
