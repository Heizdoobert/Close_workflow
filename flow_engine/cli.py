"""flow-task CLI.  flow-task "<message>"  | enqueue | status | listen | gate | doctor | exec"""

import json
import os
import shutil
import subprocess
import sys

from flow_engine import __version__, bridge, engine, gate
from flow_engine.resp import RedisError

USAGE = ('usage: flow-task "<task message>"      store a task (with FLOW_EXEC=1 also run it)\n'
         '       flow-task enqueue "<message>"   store + queue for the listener, prints the id\n'
         "       flow-task status <id> | listen | gate [dir] | doctor | exec <id> <message>")


def doctor():
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
    add("claude", bool(engine.claude_bin()), engine.claude_bin() or "not found (set FLOW_CLAUDE)")
    add("gitleaks", bool(shutil.which("gitleaks")), "found" if shutil.which("gitleaks") else "optional: secrets gate skipped", required=False)
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
        if cmd == "status" and rest:
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
    except (OSError, RedisError) as e:
        print(f"Failed to reach Redis: {e}", file=sys.stderr)
        return 1
    print(f"Task stored in Redis with ID {tid}")
    if os.environ.get("FLOW_EXEC") == "1":  # only the listener sets this: an interactive agent is already doing the task
        ok, rec = engine.execute(tid, a[0])
        return 0 if ok else 1
    return 0
