"""Scripted quality gates (0 LLM tokens). A missing optional tool means its gate falls back or skips, never a silent pass of a real finding.

Gates: secrets (gitleaks, else the built-in regex scan of the added lines), syntax of changed .py/.sh, tests (pytest if tests exist),
constraints (no weakened bar in the git diff: suppressions, removed or deleted tests, lost asserts), blast_radius (a plan step only: no undeclared
deleted or renamed path, no removed top-level Python def/class that is still referenced), critic (agent-system rules, opt-in FLOW_CRITIC=1).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from flow_engine import audit, bridge
from flow_engine.audit import is_test_file  # noqa: F401  (re-exported: the constraints gate and the tests use gate.is_test_file)
from flow_engine.secrets import redact, scan_line, secret_file_name


def _run(root, cmd, **kw):
    try:
        return subprocess.run(cmd, cwd=root, capture_output=True, text=True, **kw)
    except OSError as e:  # tool missing
        return subprocess.CompletedProcess(cmd, 127, "", str(e))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout after {kw.get('timeout')}s")


class GateError(RuntimeError):
    """A gate could not look at the change (git failed): that is a failed gate, never a silent pass."""


def _git(root, args, **kw):
    r = _run(root, ["git", *args], **kw)
    if r.returncode:
        raise GateError(f"git {args[0]} failed ({r.returncode}): {r.stderr.strip()[:120]}")
    return r


def _changed(root, *exts):
    out = _git(root, ["diff", "--name-only", "HEAD"]).stdout.split()
    out += _run(root, ["git", "ls-files", "-o", "--exclude-standard"]).stdout.split()
    return [f for f in out if f.endswith(exts) and os.path.isfile(os.path.join(root, f))]


# Markers are written with a character class (e.g. "[o]") so this line's own diff does not match the gate it defines.
WEAK = re.compile(r"^\+.*(@ts-ign[o]re|eslint-disabl[e]|type:\s*ignore|pytest\.mark\.(skip|xfail)|\bxit\(|\bit\.skip|no[q]a|no[s]ec\b|gitleaks:allo[w])", re.M)
REEXPORT = re.compile(r"no[q]a:\s*F401\b")
DROPPED = re.compile(r"^-\s*(def test_|it\(|test\()")
ASSERT = re.compile(r"^\s*assert\b")


def _per_file(diff):
    """Per file of a -U0 diff: added-minus-removed assert lines (test files only) and weak-marker hits. Hunk bodies only, so '+++'/'---' content still counts."""
    stats, cur, in_hunk = {}, None, False
    for l in diff.splitlines():
        if l.startswith("diff --git "):
            cur, in_hunk = l.rsplit(" b/", 1)[-1], False
            stats.setdefault(cur, {"asserts": 0, "weak": 0, "lines": []})
        elif l.startswith("@@"):
            in_hunk = True
        elif in_hunk and cur and l[:1] in ("+", "-"):
            if is_test_file(cur) and ASSERT.match(l[1:]):
                stats[cur]["asserts"] += 1 if l[0] == "+" else -1
            if l[0] == "+" and WEAK.match(l) and not (os.path.basename(cur) == "__init__.py" and REEXPORT.search(l)):  # `import x  # noqa: F401` is how a package re-exports
                stats[cur]["weak"] += 1
                stats[cur]["lines"].append(l[1:].strip()[:80])
    return stats


def _snapshot_diff(root, *flags):
    """Diff of the whole working tree against HEAD through a throwaway index: untracked files count as added and a plain move is a rename, not a delete."""
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(d, "index")}
        _git(root, ["read-tree", "HEAD"], env=env)
        _git(root, ["add", "-A"], env=env)
        return _git(root, ["diff", "--cached", "-M", *flags, "HEAD"], env=env).stdout


def constraints(root="."):
    """Diff-level bar: no new suppressions, no removed tests, no deleted test files, no net loss of asserts in tests. A moved test file is not a deleted one."""
    root = os.path.abspath(root)
    diff = _snapshot_diff(root, "-U0")
    dropped = [l for l in diff.splitlines() if not l.startswith("---") and DROPPED.match(l)]
    deleted = [l.split("\t", 1)[1] for l in _snapshot_diff(root, "--name-status", "--diff-filter=D").splitlines()
               if "\t" in l and is_test_file(l.split("\t", 1)[1])]
    stats = _per_file(diff)
    net = sum(s["asserts"] for f, s in stats.items() if is_test_file(f))
    files = sorted({f for f, s in stats.items() if s["weak"] or (is_test_file(f) and s["asserts"] < 0)} | set(deleted))
    hits = sum(s["weak"] for s in stats.values())
    scanner = [f for f in _changed_names(root) if os.path.basename(f).startswith(".gitleaks")]  # .gitleaks.toml / .gitleaksignore can hide a key
    files = sorted(set(files) | set(scanner))
    ok = not hits and not dropped and not deleted and net >= 0 and not scanner
    findings = [f"{f} adds a suppression marker: {l}" for f, s in stats.items() for l in s["lines"]] + [f"removed test line: {l[1:60]}" for l in dropped] + [f"{d} was deleted" for d in deleted] + [f"{f} changes the secret scanner config" for f in scanner]
    if net < 0:
        findings.append(f"net {net} assert lines in test files: put the assertions back")
    return {"ok": ok, "weakened": hits, "removed_tests": len(dropped), "assert_net": net,
            "deleted_tests": deleted, "files": files, "findings": [redact(x) for x in findings[:10]]}


def _changed_names(root, ref="HEAD"):
    """Every file the diff against `ref` touches, plus untracked files (NUL-separated, so odd names survive)."""
    out = _git(root, ["diff", "--name-only", "-z", ref]).stdout.split("\0")
    out += _run(root, ["git", "ls-files", "-o", "--exclude-standard", "-z"]).stdout.split("\0")
    return sorted({f for f in out if f})


def _deleted(root, ref="HEAD"):
    """Paths the diff against `ref` deletes or renames away."""
    return {l.split("\t")[1] for l in _git(root, ["diff", "--name-status", "-M", ref]).stdout.splitlines() if l[:1] in ("D", "R") and "\t" in l}


def _added_lines(root, ref="HEAD"):
    """(file, line number, text) for each added line of the diff against `ref`, plus every text line of each untracked file. Binary files are skipped."""
    lines, cur, new_no, in_hunk = [], None, 0, False
    for l in _git(root, ["diff", ref, "-U0"]).stdout.splitlines():
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


DEF = re.compile(r"(?:async\s+def|def|class)\s+([A-Za-z_]\w{2,})")


def blast_radius(root, allowed):
    """Step 5 of the method, as far as code can see it: the diff may not delete or rename a path the step did not declare (`allowed`), nor remove a
    top-level Python def/class that is still referenced somewhere in the tree (a move to another file is fine). Methods and signatures are not checked."""
    root = os.path.abspath(root)
    allowed = set(allowed)
    gone = []
    for line in _git(root, ["diff", "--name-status", "-M", "HEAD"]).stdout.splitlines():
        kind, *paths = line.split("\t")
        if kind[:1] in ("D", "R") and paths[0] not in allowed and "__pycache__" not in paths[0] and not paths[0].endswith(".pyc"):
            gone.append(paths[0])
    readded = {m.group(1) for _f, _n, text in _added_lines(root) if (m := DEF.match(text))}
    removed = {m.group(1) for l in _git(root, ["diff", "HEAD", "-U0", "-M", "--", "*.py"]).stdout.splitlines() if l.startswith("-") and (m := DEF.match(l[1:]))} - readded
    orphaned = []
    for name in sorted(removed):
        users = _run(root, ["git", "grep", "-l", "-w", "-F", "--untracked", "-e", name, "--", "*.py"]).stdout.split()
        if users:
            orphaned.append(f"{name} (removed, still used in {', '.join(users[:3])})")
    findings = [f"undeclared removal or rename: {p}" for p in gone] + orphaned
    return {"ok": not findings, "findings": findings[:10]}


def run_gates(root=".", allowed=None):
    """allowed = the files a plan step declared; given, the blast_radius, security (hardening) and code_verify gates run too."""
    try:
        return _run_gates(os.path.abspath(root), allowed)
    except GateError as e:  # git could not show the change: nothing was checked, so nothing passes
        return {"ok": False, "gates": {"git": {"ok": False, "findings": [str(e)]}}}


def _run_gates(root, allowed):
    gates = {}

    if shutil.which("gitleaks"):
        with tempfile.TemporaryDirectory() as td:
            rep = os.path.join(td, "r.json")
            cfg = os.path.join(td, "gitleaks.toml")
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write("[extend]\nuseDefault = true\n")  # pinned: a .gitleaks.toml in the tree cannot allowlist its own keys
            r = _run(root, ["gitleaks", "dir", ".", "--no-banner", "--redact", "--ignore-gitleaks-allow", "-c", cfg, "-r", rep, "-f", "json"], timeout=120)
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
    if allowed is not None:
        gates["blast_radius"] = blast_radius(root, allowed)
        added = _added_lines(root)
        for name, r in (("security", audit.hardening(added, _changed_names(root))), ("code_verify", audit.verify(root, added, allowed, _deleted(root)))):
            gates[name] = {"ok": not r["hard"], "findings": r["hard"][:10], "soft": len(r["soft"])}

    if os.environ.get("FLOW_CRITIC") == "1" and bridge.agent_system():
        fails = [f for f in _changed(root, ".py") if bridge.run_agent("critic", "--file", f, cwd=root).returncode]
        gates["critic"] = {"ok": not fails, "files": fails[:10]}

    return {"ok": all(g["ok"] for g in gates.values()), "gates": gates}
