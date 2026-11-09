"""Scripted quality gates (0 LLM tokens). A missing optional tool means its gate falls back or skips, never a silent pass of a real finding.

Gates: secrets (gitleaks, else the built-in regex scan of the added lines), syntax of changed .py/.sh, tests (pytest if tests exist),
constraints (no weakened bar in the git diff: suppressions, removed or deleted tests, lost asserts), critic (agent-system rules, opt-in FLOW_CRITIC=1).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from flow_engine import bridge
from flow_engine.secrets import scan_line, secret_file_name


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


# Markers are written with a character class (e.g. "[o]") so this line's own diff does not match the gate it defines.
WEAK = re.compile(r"^\+.*(@ts-ign[o]re|eslint-disabl[e]|type:\s*ignore|pytest\.mark\.(skip|xfail)|\bxit\(|\bit\.skip|no[q]a)", re.M)
DROPPED = re.compile(r"^-\s*(def test_|it\(|test\()")
ASSERT = re.compile(r"^\s*assert\b")


def is_test_file(f):
    return f.endswith(("_test.py",)) or os.path.basename(f).startswith("test_") or "tests" in f.split("/")[:-1]


def _per_file(diff):
    """Per file of a -U0 diff: added-minus-removed assert lines (test files only) and weak-marker hits. Hunk bodies only, so '+++'/'---' content still counts."""
    stats, cur, in_hunk = {}, None, False
    for l in diff.splitlines():
        if l.startswith("diff --git "):
            cur, in_hunk = l.rsplit(" b/", 1)[-1], False
            stats.setdefault(cur, {"asserts": 0, "weak": 0})
        elif l.startswith("@@"):
            in_hunk = True
        elif in_hunk and cur and l[:1] in ("+", "-"):
            if is_test_file(cur) and ASSERT.match(l[1:]):
                stats[cur]["asserts"] += 1 if l[0] == "+" else -1
            if l[0] == "+" and WEAK.match(l):
                stats[cur]["weak"] += 1
    return stats


def constraints(root="."):
    """Diff-level bar: no new suppressions, no removed tests, no deleted test files, no net loss of asserts in tests."""
    root = os.path.abspath(root)
    diff = _run(root, ["git", "diff", "HEAD", "-U0"]).stdout
    hits = WEAK.findall(diff)
    dropped = [l for l in diff.splitlines() if not l.startswith("---") and DROPPED.match(l)]
    deleted = [l.split("\t", 1)[1] for l in _run(root, ["git", "diff", "--name-status", "HEAD", "--diff-filter=D"]).stdout.splitlines()
               if "\t" in l and is_test_file(l.split("\t", 1)[1])]
    stats = _per_file(diff)
    net = sum(s["asserts"] for f, s in stats.items() if is_test_file(f))
    files = sorted({f for f, s in stats.items() if s["weak"] or (is_test_file(f) and s["asserts"] < 0)} | set(deleted))
    ok = not hits and not dropped and not deleted and net >= 0
    return {"ok": ok, "weakened": len(hits), "removed_tests": len(dropped), "assert_net": net,
            "deleted_tests": deleted, "files": files}


def _changed_names(root):
    """Every file the diff against HEAD touches, plus untracked files (NUL-separated, so odd names survive)."""
    out = _run(root, ["git", "diff", "--name-only", "-z", "HEAD"]).stdout.split("\0")
    out += _run(root, ["git", "ls-files", "-o", "--exclude-standard", "-z"]).stdout.split("\0")
    return sorted({f for f in out if f})


def _added_lines(root):
    """(file, line number, text) for each added line of the diff against HEAD, plus every text line of each untracked file. Binary files are skipped."""
    lines, cur, new_no, in_hunk = [], None, 0, False
    for l in _run(root, ["git", "diff", "HEAD", "-U0"]).stdout.splitlines():
        if l.startswith("diff --git "):
            cur, in_hunk = l.rsplit(" b/", 1)[-1], False
        elif l.startswith("@@"):
            m = re.search(r"\+(\d+)", l)
            in_hunk, new_no = True, int(m.group(1)) if m else 0
        elif in_hunk and cur and l.startswith("+"):
            lines.append((cur, new_no, l[1:]))
            new_no += 1
    for f in _run(root, ["git", "ls-files", "-o", "--exclude-standard", "-z"]).stdout.split("\0"):
        path = os.path.join(root, f)
        if not f or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        if b"\0" in data[:8000]:
            continue
        lines += [(f, i, text) for i, text in enumerate(data.decode("utf-8", "replace").splitlines(), 1)]
    return lines


def secrets_builtin(root="."):
    """Regex fallback for the secrets gate when gitleaks is missing. Findings are 'file:line rule' or 'file secret-file-name'; matched text is never stored."""
    root = os.path.abspath(root)
    findings = [f"{f}:{n} {rule}" for f, n, text in _added_lines(root) for rule in scan_line(text)]
    findings += [f"{f} secret-file-name" for f in _changed_names(root) if secret_file_name(f)]
    return {"ok": not findings, "engine": "builtin", "findings": findings[:10]}


def run_gates(root="."):
    root = os.path.abspath(root)
    gates = {}

    if shutil.which("gitleaks"):
        with tempfile.TemporaryDirectory() as td:
            rep = os.path.join(td, "r.json")
            r = _run(root, ["gitleaks", "dir", ".", "--no-banner", "--redact", "-r", rep, "-f", "json"], timeout=120)
            found = json.load(open(rep)) if os.path.exists(rep) and os.path.getsize(rep) else []
            gates["secrets"] = {"ok": r.returncode == 0, "engine": "gitleaks", "findings": [f"{f['File']}:{f['StartLine']} {f['RuleID']}" for f in found][:10]}
    else:
        gates["secrets"] = secrets_builtin(root)

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

    gates["constraints"] = constraints(root)

    if os.environ.get("FLOW_CRITIC") == "1" and bridge.agent_system():
        fails = [f for f in _changed(root, ".py") if bridge.run_agent("critic", "--file", f, cwd=root).returncode]
        gates["critic"] = {"ok": not fails, "files": fails[:10]}

    return {"ok": all(g["ok"] for g in gates.values()), "gates": gates}
