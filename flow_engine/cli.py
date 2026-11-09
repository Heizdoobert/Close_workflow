"""flow-task CLI.  flow-task "<message>"  | enqueue | status | listen | gate | doctor | exec"""

import json
import os
import shutil
import subprocess
import sys

from flow_engine import __version__, agents, bridge, engine, gate, plan, sandbox
from flow_engine.resp import RedisError

USAGE = ('usage: flow-task "<task message>"      store a task (with FLOW_EXEC=1 also run it)\n'
         '       flow-task enqueue "<message>"   store + queue for the listener, prints the id\n'
         "       flow-task status <id> | listen | gate [dir] | doctor | exec <id> <message>\n"
         '       flow-task plan "<goal>" [--model <your model id>]   draft a plan (nothing runs until approve)\n'
         "       flow-task where [<plan>]   where a plan stands and the next action (default: newest plan of this repo)\n"
         "       flow-task show <plan> | approve <plan> [--wait] | retry <plan> | reject <plan> | report <plan> | run <plan> | plans\n"
         "       flow-task recover   resume plans whose runner died (no listener needed) and kill their orphan agent groups\n"
         "       flow-task clean [--older-than DAYS] [--result-branches]   remove finished plans' worktrees and flowstep/* branches (never a running plan)\n"
         "       flow-task doctor also reports the sandbox (bwrap) and gitleaks lines: a missing one is a warning, not a failure")


def doctor_checks():
    """The health checks as (name, ok, detail, required) tuples; doctor() prints them."""
    checks = []

    def add(name, ok, detail, required=True):
        checks.append((name, ok, detail, required))

    add("python", sys.version_info >= (3, 9), sys.version.split()[0])
    add("env", bool(os.environ.get("REDIS_PASSWORD")), str(engine.ROOT / ".env") if os.environ.get("REDIS_PASSWORD") else "no REDIS_PASSWORD (python install.py creates .env)")
    try:
        add("redis", engine.redis().call("PING") == "PONG", f"{os.environ.get('REDIS_HOST', 'localhost')}:{os.environ.get('REDIS_PORT', 6379)}")
    except (OSError, RedisError) as e:
        add("redis", False, f"{type(e).__name__}: {e}"[:150])
    up = engine.listener_running()
    add("listener", up, "running" if up else "stopped (flow-task listen, or install.py --service)", required=False)
    add("claude", bool(agents.claude_bin()), agents.claude_bin() or "not found (set FLOW_CLAUDE)")
    sb = sandbox.available()
    add("sandbox", sb is not None, sb or "none: sub-agent writes only checked after each step", required=False)
    gl = shutil.which("gitleaks")
    add("gitleaks", bool(gl), "found" if gl else "missing: secrets gate uses the built-in regex scan only", required=False)
    exe = bridge.agent_system()
    add("agent-system", bool(exe), exe or "not found: memory recall/learn disabled", required=False)
    if exe:
        p = bridge.run_agent("doctor", "--json")
        try:
            rep = json.loads(p.stdout)
            bad = [c["name"] for c in rep["checks"] if not c["ok"] and c["required"]]
            add("agent doctor", rep["ok"], "ok" if rep["ok"] else "failing: " + ",".join(bad), required=False)
        except Exception:
            add("agent doctor", False, "no report", required=False)
    return checks


def doctor():
    checks = doctor_checks()
    for name, ok, detail, req in checks:
        print(f"{'OK  ' if ok else ('FAIL' if req else 'warn')}  {name:<13} {detail}")
    return 0 if all(ok for _, ok, _, req in checks if req) else 1


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if a else 1
    if a[0] == "--version":
        print(f"flow-task {__version__}")
        return 0
    cmd, rest = a[0], a[1:]
    try:
        if cmd == "doctor":
            return doctor()
        if cmd == "listen":
            return engine.listen()
        if cmd == "gate":
            res = gate.run_gates(rest[0] if rest else ".")
            print(json.dumps(res, ensure_ascii=False))
            return 0 if res["ok"] else 1
        if cmd == "plan" and rest:
            model = rest[rest.index("--model") + 1] if "--model" in rest[:-1] else None
            goal = " ".join(r for i, r in enumerate(rest) if r != "--model" and (i == 0 or rest[i - 1] != "--model"))
            pid = plan.create(goal, model)
            print(plan.show(pid))
            return 0
        if cmd == "where" and len(rest) <= 1:  # more words would be a task message ("where is the bug"): falls through to submit
            print(plan.where(rest[0] if rest else None))
            return 0
        if cmd == "show" and rest:
            print(plan.show(rest[0]))
            return 0
        if cmd == "approve" and rest:
            out = plan.approve(rest[0], "--wait" in rest)
            for o in plan.load(rest[0])[2].get("overlap", []):
                print(f"warning: plan {rest[0]} shares files with plan {o['plan']}: {', '.join(o['files'])} (merging both may conflict)", file=sys.stderr)
            print(out if out != "started" else f"approved: running in the background. Check: flow-task status {rest[0]}")
            return 0 if out in ("started", "done") else 1
        if cmd == "retry" and rest:
            plan.retry(rest[0])
            print(f"retrying the failed steps in the background. Check: flow-task status {rest[0]}")
            return 0
        if cmd == "reject" and rest:
            plan.reject(rest[0])
            return 0
        if cmd == "run" and rest:
            out = plan.run(rest[0])
            print(out)
            return 0 if out in ("done", "already running") else 1
        if cmd == "report" and rest:
            print(plan.report(rest[0]).read_text(encoding="utf-8"))
            return 0
        if cmd == "clean":
            older = None
            if "--older-than" in rest:
                try:
                    older = float(rest[rest.index("--older-than") + 1])
                except (IndexError, ValueError):
                    raise plan.PlanError("usage: flow-task clean [--older-than DAYS] [--result-branches]")
            done = plan.clean(older, "--result-branches" in rest)
            print("\n".join(done) if done else "nothing to clean")
            return 0
        if cmd == "recover":
            lines = plan.recover()
            print("\n".join(lines) if lines else "nothing to recover")
            return 0
        if cmd == "plans":
            for sf in sorted(plan.plans_dir().glob("*/state.json")):
                st = json.loads(sf.read_text(encoding="utf-8"))
                print(f"{st['id']}  {st['status']:<18} {st['goal'][:70]}")
            return 0
        if cmd == "status" and rest:
            if (plan.plans_dir() / rest[0] / "state.json").is_file():
                print(json.dumps(plan.status(rest[0]), ensure_ascii=False))
                return 0
            print(json.dumps(engine.status(rest[0]), ensure_ascii=False))
            return 0
        if cmd == "enqueue" and rest:
            print(engine.enqueue(rest[0]))
            return 0
        if cmd == "exec" and len(rest) == 2:
            ok, rec = engine.execute(rest[0], rest[1])
            print(f"{'done' if ok else 'failed'} task {rest[0]} ({rec['attempts']} attempt(s), ${rec['cost_usd']})")
            return 0 if ok else 1
        tid = engine.submit(a[0])  # anything else is the task message
    except plan.PlanError as e:
        print(f"plan error: {e}", file=sys.stderr)
        return 1
    except (OSError, RedisError) as e:
        print(f"Failed to reach Redis: {e}", file=sys.stderr)
        return 1
    print(f"Task stored in Redis with ID {tid}")
    if os.environ.get("FLOW_EXEC") == "1":  # only the listener sets this: an interactive agent is already doing the task
        ok, rec = engine.execute(tid, a[0])
        return 0 if ok else 1
    return 0
