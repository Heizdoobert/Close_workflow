"""`flow-task selftest`: the whole plan flow once, for real, in a throwaway git repo, so an agent that just installed this knows it works on this machine before it touches a real project.

It spends real tokens (about 0.03 USD: a planner, three parallel step agents, a review and three ship personas) and refuses to run past FLOW_PLAN_MAX_USD (0.5 here). It exercises what a
unit test with fake agents cannot: the real `claude`, the sandbox, git worktrees on this filesystem (the repo lives in the temp dir, plans/ lives in the install folder: possibly two
filesystems), merging three parallel steps, the integration re-run, review and SHIP. It does not test the interview (that needs a person) or any other agent backend.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from flow_engine import plan

GOAL = ("Add three independent stdlib-only modules to this repo, each in its own file with its own test file: slug.py (slugify(text) lowercases, replaces non-alphanumerics with single "
        "hyphens, strips hyphens), wordcount.py (top_words(text, n) returns the n most common words, case-insensitive, ties alphabetical), roman.py (to_roman(n) for 1..3999, ValueError "
        "outside the range). Tests must use assert.")


def _git(cwd, *a):
    return subprocess.run(["git", *plan.IDENT, *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def run(model=None, keep=False, out=print):
    """Returns 0 when every check passed. Each check is printed as OK or FAIL with what was seen."""
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        out(f"{'OK  ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        return ok

    tmp = Path(tempfile.mkdtemp(prefix="flow-selftest-"))
    saved = {k: os.environ.get(k) for k in ("FLOW_WORKDIR", "FLOW_PLAN_MAX_USD")}
    pid = None
    try:
        _git(tmp, "init", "-q")
        _git(tmp, "checkout", "-q", "-b", "main")
        (tmp / "README.md").write_text("# selftest\n", encoding="utf-8")
        _git(tmp, "add", "-A")
        check("throwaway git repo", _git(tmp, "commit", "-q", "-m", "init").returncode == 0, str(tmp))
        os.environ["FLOW_WORKDIR"] = str(tmp)
        os.environ.setdefault("FLOW_PLAN_MAX_USD", "0.5")
        pid = plan.create(GOAL, model)
        d, p, _ = plan.load(pid)
        check("plan drafted", len(p["steps"]) >= 2, f"{len(p['steps'])} steps")
        check("analysis block present (research, root cause, fix, blast radius, spec)", bool(p.get("analysis")))
        check("steps can run in parallel (a step without dependencies besides the first)", sum(not s["deps"] for s in p["steps"]) >= 2)
        res = plan.approve(pid, wait_for=True)
        _, _, st = plan.load(pid)
        check("every step merged", res == "done" and all(e["status"] == "done" for e in st["steps"].values()),
              ", ".join(f"{k}:{v['status']}" for k, v in st["steps"].items()) + (f"; {st.get('error')}" if st.get("error") else ""))
        check("integration re-run on the merged branch passed", bool((st.get("integration") or {}).get("ok")))
        files = set(_git(tmp, "ls-tree", "-r", "--name-only", f"flow/{pid}").stdout.split())
        check("result branch holds the new modules", {"slug.py", "wordcount.py", "roman.py"} <= files, f"flow/{pid}")
        check("your branch was not touched", _git(tmp, "log", "--oneline", "main").stdout.count("\n") == 1)
        rep = (d / "report.md").read_text(encoding="utf-8") if (d / "report.md").exists() else ""
        rows = [x.split("|")[1].strip() for x in rep.splitlines() if x.startswith("| ") and x.split("|")[1].strip().isdigit()]
        check("report has the 16-step coverage table", rows == [str(i) for i in range(1, 17)], f"{len(rows)} rows")
        ship = (st.get("ship") or {}).get("decision")
        check("SHIP ran and decided", ship in ("GO", "NO-GO"), str(ship))
        out(f"cost {st.get('plan_cost_usd', '?')} plan + steps {st.get('cost_usd', '?')} USD; plan {pid}")
    except plan.PlanError as e:
        check("the flow ran to the end", False, str(e)[:300])
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        if pid and not keep:
            shutil.rmtree(plan.plans_dir() / pid, ignore_errors=True)
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    out("selftest " + ("PASSED" if results and all(results) else "FAILED"))
    return 0 if results and all(results) else 1
