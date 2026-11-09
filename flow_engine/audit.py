"""Security hardening (agent-skills security-and-hardening) and code verification (against sloppy code), as 0-token scans of the lines a change adds.

Each returns {"hard": [...], "soft": [...]} of "file:line rule" strings (the matched text is never echoed). hard = the gate fails and the step agent is told to fix
it; soft = shown in the report and under "should fix" in the ship decision. Markers are written with a character class (e.g. "[o]") so this file's own lines do not
trip the scan it defines. Coverage is partial on purpose and the report says so: patterns of dangerous code, not an audit of authentication, rate limits, SSRF or
resource ownership.
"""

import ast
import os
import re

TEXT_SKIP = re.compile(r"(?i)\.(md|rst|txt|json|lock)$")  # prose and data mention these words legitimately
DEPS = re.compile(r"(?i)(^|/)(requirements[^/]*\.txt|pyproject\.toml|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|Pipfile(\.lock)?|poetry\.lock|go\.(mod|sum)|Cargo\.(toml|lock))$")
SQL = r"(?i)^(?=.*(execute|query|cursor|\bsql))(?=.*\b(select\s.+\sfrom|insert\s+into|delete\s+from|update\s+\w+\s+set)\b).*(\{[^}]*\}|[\"']\s*\+|\.format\()"

HARD = [(r, re.compile(x)) for r, x in [
    ("eval/exec of a string", r"(?<![\w.])(ev[a]l|ex[e]c)\("),
    ("shell injection (shell=True, os.system, os.popen)", r"shell\s*=\s*True|\bos\.(sy[s]tem|po[p]en)\("),
    ("unsafe deserialisation (pickle, marshal, yaml.load)", r"\b(pickle|marshal)\.loads?\(|\byaml\.load\((?!.*Safe)"),
    ("TLS verification switched off", r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|_create_unverified_conte[x]t"),
    ("SQL built from a string (use parameters)", SQL),
    ("predictable temp file (tempfile.mktemp)", r"tempfile\.mkt[e]mp\("),
    ("world-writable permissions", r"chmod[^\n]*\b0?o?77[7]\b"),
    ("cookie flag weakened (httponly, secure, samesite)", r"(?i)(http_?only|secure)\s*[=:]\s*(False|false)|samesite\s*[=:]\s*[\"']?none"),
    ("XSS sink (innerHTML, raw HTML, |safe)", r"\.innerHTML\s*=|dangerouslySetInner[H]TML|document\.write\(|\|\s*safe\b"),
]]
SOFT = [(r, re.compile(x)) for r, x in [
    ("destructive call on a derived path: resolve it and check an allowlisted root first", r"\b(rmtree|unlink|rmdir)\(\s*[^\"'\s)]|\bos\.remove\(\s*[^\"'\s)]"),
    ("weak hash (md5, sha1): not for passwords or integrity against an attacker", r"hashlib\.(md5|sha1)\("),
    ("DEBUG switched on", r"^\s*DEBUG\s*=\s*True"),
    ("CORS allows every origin", r"(?i)allow-origin[\"']?\s*[:,=]\s*[\"']\*"),
    ("binds every network interface", r"[\"']0\.0\.0\.0[\"']"),
]]
SLOP_HARD = [(r, re.compile(x)) for r, x in [
    ("debugger left in", r"\bbreakp[o]int\(\)|\bpdb\.set_tra[c]e|^\s*import\s+i?pdb\b|^\s*debugg[e]r;"),
    ("bare except swallows every error", r"^\s*except\s*:"),
]]
SLOP_SOFT = [(r, re.compile(x)) for r, x in [
    ("unfinished marker", r"\b(TOD[O]|FIXM[E]|XX[X]|HAC[K])\b"),
    ("console output left in", r"console\.(log|debug)\("),
]]
DEAD_CODE = re.compile(r"^\s*#\s*(def |class |import |from \w+ import |return\b|print\(|if .+:$|for .+:$)")


def is_test_file(f):
    return f.endswith("_test.py") or os.path.basename(f).startswith("test_") or "tests" in f.split("/")[:-1]


def hardening(added, changed):
    """Step 10. `added` = (file, line, text) of the added lines; `changed` = every changed file name."""
    hard, soft = [], []
    for f, n, text in added:
        if TEXT_SKIP.search(f) or is_test_file(f):
            continue
        hard += [f"{f}:{n} {rule}" for rule, rx in HARD if rx.search(text)]
        soft += [f"{f}:{n} {rule}" for rule, rx in SOFT if rx.search(text)]
    soft += [f"{f} dependency manifest changed: run pip-audit or npm audit on the lockfile (not done here)" for f in changed if DEPS.search(f)]
    return {"hard": hard, "soft": soft}


def _has_assert(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call):
            name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            if re.search(r"(?i)assert|raises|warns|fail", name):
                return True
    return False


def _stub(fn):
    body = [s for i, s in enumerate(fn.body) if not (i == 0 and isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant) and isinstance(s.value.value, str))]
    if len(body) != 1:
        return False
    s = body[0]
    if isinstance(s, ast.Pass) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and s.value.value is Ellipsis):
        return True
    return isinstance(s, ast.Raise) and "NotImplementedError" in ast.dump(s.exc or ast.Pass())


def _exempt(fn, owner):
    deco = " ".join(ast.unparse(d) for d in fn.decorator_list)
    bases = " ".join(ast.unparse(b) for b in owner.bases) if isinstance(owner, ast.ClassDef) else ""
    return bool(re.search(r"abstract|overload", deco) or re.search(r"Protocol|ABC", bases))


def _py_findings(f, src, nums, hard, soft):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return  # the syntax gate reports it
    owner = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {s.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in n.targets)
                                                                           for s in ast.walk(n.value) if isinstance(s, ast.Constant) and isinstance(s.value, str)}
    lines = src.splitlines()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno in nums:
            if is_test_file(f) and n.name.startswith("test") and not _has_assert(n):
                hard.append(f"{f}:{n.lineno} test has no assertion")
            elif not is_test_file(f) and _stub(n) and not _exempt(n, owner.get(id(n))):
                hard.append(f"{f}:{n.lineno} stub, the function does nothing ({n.name})")
        elif isinstance(n, ast.ExceptHandler) and n.lineno in nums and n.type is not None and all(isinstance(s, ast.Pass) for s in n.body) and re.search(r"\b(Base)?Exception\b", ast.unparse(n.type)):
            hard.append(f"{f}:{n.lineno} except Exception: pass swallows every error")
    if os.path.basename(f) != "__init__.py":
        for n in tree.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno in nums and "noqa" not in lines[n.lineno - 1]:
                for a in n.names:
                    name = a.asname or a.name.split(".")[0]
                    if name != "*" and name not in used:
                        soft.append(f"{f}:{n.lineno} unused import {name}")


def verify(root, added, declared, deleted):
    """Step 13: what sloppy code leaves behind in the lines a change adds. `declared` = files the plan said would exist; `deleted` = files the diff removes on purpose."""
    hard, soft, by_file = [], [], {}
    for f, n, text in added:
        if not TEXT_SKIP.search(f):
            by_file.setdefault(f, {})[n] = text
    for f, lines in by_file.items():
        for n, text in lines.items():
            hard += [f"{f}:{n} {rule}" for rule, rx in SLOP_HARD if rx.search(text)]
            soft += [f"{f}:{n} {rule}" for rule, rx in SLOP_SOFT if rx.search(text)]
        if sum(bool(DEAD_CODE.match(t)) for t in lines.values()) >= 2:
            soft.append(f"{f} commented-out code added: delete it, git keeps the history")
        if f.endswith(".py"):
            try:
                src = open(os.path.join(root, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            _py_findings(f, src, set(lines), hard, soft)
    for d in sorted(declared):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            continue
        if not os.path.exists(p):
            if d not in deleted:
                hard.append(f"{d} was declared by the plan but does not exist")
        elif not open(p, "rb").read().strip():
            hard.append(f"{d} was declared by the plan but is empty")
        elif d.endswith(".py"):
            try:
                if not ast.parse(open(p, encoding="utf-8").read()).body:
                    hard.append(f"{d} was declared by the plan but holds no code")
            except (SyntaxError, UnicodeDecodeError):
                pass
    return {"hard": hard, "soft": soft}
