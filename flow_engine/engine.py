"""Tasks in Redis, one gated `claude -p` run per task, and the file-drop listener."""

import codecs
import datetime
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from flow_engine import bridge, gate
from flow_engine.resp import Redis

ROOT = Path(os.environ.get("FLOW_HOME") or Path(__file__).resolve().parent.parent)
INCOMING, LOGS = ROOT / "incoming_tasks", ROOT / "logs"
LOG = LOGS / "flow_engine.log"
TTL = int(os.environ.get("TASK_TTL", "2592000"))


def load_env():
    """Read ROOT/.env into os.environ without overriding what the caller already set."""
    f = ROOT / ".env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip())


load_env()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def redis():
    return Redis(os.environ.get("REDIS_HOST", "localhost"), os.environ.get("REDIS_PORT", 6379), os.environ.get("REDIS_PASSWORD") or None)


def log(line):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def submit(message):
    """Store a queued task; returns its id."""
    tid = str(time.time_ns())
    payload = json.dumps({"task_id": tid, "message": message, "status": "queued", "timestamp": now()}, ensure_ascii=False)
    redis().call("SET", f"task:{tid}", payload, "EX", TTL)
    log(f"[{now()}] [TASK:{tid}] {message} -> stored")
    return tid


def enqueue(message):
    """Store a task and drop it in incoming_tasks/ for the listener to execute."""
    tid = submit(message)
    INCOMING.mkdir(parents=True, exist_ok=True)
    tmp = INCOMING / f"{tid}.tmp"  # rename so the listener never reads a half-written file
    tmp.write_text(message, encoding="utf-8")
    os.replace(tmp, INCOMING / f"{tid}.txt")
    return tid


def status(tid):
    h = redis().call("HGETALL", f"taskstate:{tid}") or []
    d = dict(zip(h[::2], h[1::2]))
    if not d:
        d = {"status": "queued"} if redis().call("EXISTS", f"task:{tid}") else {"status": "unknown"}
    d["result"] = d.get("result", "")[:1000]
    return d


def claude_bin():
    if os.environ.get("FLOW_CLAUDE"):
        return os.environ["FLOW_CLAUDE"]
    real = Path.home() / ".local" / "bin" / "claude.real"  # host binary; plain `claude` may be a Docker wrapper
    return str(real) if real.exists() else shutil.which("claude")


def _claude(prompt, budget, workdir):
    # ponytail: --setting-sources project + strict MCP + no slash commands cuts a ~125k-token
    # session preamble to ~7k; add flags back here only if a task needs user plugins/MCP.
    exe = claude_bin()
    if not exe:
        raise FileNotFoundError("claude CLI not found (install it or set FLOW_CLAUDE)")
    p = subprocess.run(
        [exe, "-p", "--model", os.environ.get("FLOW_MODEL", "haiku"), "--output-format", "json", "--no-session-persistence",
         "--max-budget-usd", f"{budget:.2f}", "--setting-sources", "project", "--strict-mcp-config",
         "--disable-slash-commands", "--tools", os.environ.get("FLOW_TOOLS", "Read,Grep,Glob")],  # read-only by default
        input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=int(os.environ.get("FLOW_TIMEOUT", "600")), cwd=workdir)
    out = json.loads(p.stdout)
    r = [e for e in out if e.get("type") == "result"][-1] if isinstance(out, list) else out
    return p.returncode == 0 and not r.get("is_error"), r


def _failed(gate_out):
    try:
        return [n for n, g in json.loads(gate_out)["gates"].items() if not g["ok"]]
    except Exception:
        return ["unknown"]


def execute(tid, message):
    """Run one task. FLOW_GATE=1: gate after each attempt, retry with the gate output (FLOW_RETRIES) inside one budget."""
    workdir = os.environ.get("FLOW_WORKDIR", str(ROOT))
    budget, gated = float(os.environ.get("FLOW_MAX_USD", "0.50")), os.environ.get("FLOW_GATE") == "1"
    retries, model = int(os.environ.get("FLOW_RETRIES", "2")), os.environ.get("FLOW_MODEL", "haiku")
    key, r_ = f"taskstate:{tid}", redis()

    def hset(**kv):
        r_.call("HSET", key, *[x for k, v in kv.items() for x in (k, v)])
        r_.call("EXPIRE", key, TTL)

    hset(status="running", started=now(), model=model)
    t0 = time.time()
    rec = {"event": "exec", "task_id": tid, "model": model}
    tok = dict(input_tokens=0, cache_read=0, cache_create=0, output_tokens=0)
    spent, attempt, ok, r, error, gate_out, first_fail = 0.0, 0, False, {}, None, "", ""
    prompt = bridge.recall(message) + message
    while True:
        attempt += 1
        gate_failed = False
        try:
            ok, r = _claude(prompt, max(budget - spent, 0.01), workdir)
            u = r.get("usage", {})
            spent += r.get("total_cost_usd", 0)
            for k, src in (("input_tokens", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
                           ("cache_create", "cache_creation_input_tokens"), ("output_tokens", "output_tokens")):
                tok[k] += u.get(src, 0)
        except Exception as e:  # timeout, bad JSON, missing binary: record, never drop silently
            ok, error = False, f"{type(e).__name__}: {e}"[:300]
        if ok and gated:
            g = gate.run_gates(workdir)
            gate_out = json.dumps(g, ensure_ascii=False)
            gate_failed, ok = not g["ok"], g["ok"]
        if ok or not gate_failed or attempt > retries or spent >= budget:
            break
        first_fail = first_fail or gate_out
        prompt = f"{message}\n\nYour previous attempt failed these automated checks. Fix them:\n{gate_out[:1500]}"

    result = (r.get("result") or "")[:4000]
    rec.update(status="done" if ok else "failed", duration_ms=int((time.time() - t0) * 1000), cost_usd=round(spent, 6), attempts=attempt, **tok)
    if error:
        rec["error"] = error
    if gated and gate_out:
        rec["gate_ok"] = not gate_failed
        if attempt > 1:  # a retry happened: keep the lesson so recall finds it next time
            outcome = "still failing after" if gate_failed else "fixed by"
            bridge.record_recipe(",".join(_failed(first_fail)), f"task '{message[:80]}': {outcome} {attempt} attempts; first failure {first_fail[:300]}")
    if ok:
        bridge.learn(tid, message, result)
    hset(**{k: v for k, v in rec.items() if k not in ("event", "task_id")}, result=result, gate=gate_out[:1000], finished=now())
    log(json.dumps({"ts": now(), **rec}, ensure_ascii=False))
    return ok, rec


def _read_task(path):
    data = path.read_bytes()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):  # PowerShell `>` writes UTF-16
        return data.decode("utf-16").strip()
    return data.decode("utf-8-sig", "replace").strip()


def _single_instance():
    """Hold an OS file lock for the process lifetime (released automatically on crash). None = already running."""
    f = open(ROOT / ".listener.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def listener_running():
    lock = _single_instance()
    if lock:
        lock.close()
    return lock is None


def _process(path, redis_):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = INCOMING / "failed"
    message = _read_task(path)
    if message:
        try:
            tid = path.stem if path.stem.isdigit() and redis_.call("EXISTS", f"task:{path.stem}") else submit(message)
            ok, rec = execute(tid, message)
            dest = INCOMING / ("processed" if ok else "failed")
            (LOGS / f"{path.stem}_{stamp}.log").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{'done' if ok else 'failed'} task {tid} ({rec['attempts']} attempt(s), ${rec['cost_usd']})", flush=True)
        except Exception as e:
            print(f"error on {path.name}: {type(e).__name__}: {e}", flush=True)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest / f"{stamp}_{path.name}"))


def listen(poll=2.0):
    lock = _single_instance()
    if not lock:
        print("listener already running")
        return 0
    for d in (INCOMING / "processed", INCOMING / "failed", LOGS):
        d.mkdir(parents=True, exist_ok=True)
    print(f"listener watching {INCOMING}", flush=True)
    r = redis()
    try:
        while True:
            # ponytail: tasks run one at a time; add a worker pool if queue depth matters
            for p in sorted(INCOMING.glob("*.txt"), key=lambda p: p.stat().st_mtime):
                if time.time() - p.stat().st_mtime > 1:  # still being written otherwise
                    _process(p, r)
            time.sleep(poll)
    except KeyboardInterrupt:
        return 0
