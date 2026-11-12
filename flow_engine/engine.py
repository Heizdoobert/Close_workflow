"""Tasks in Redis, one gated `claude -p` run per task, and the file-drop listener."""

import codecs
import datetime
import json
import os
import shutil
import time
from pathlib import Path

from flow_engine import agents, bridge, gate
from flow_engine.resp import Redis, RedisError

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


def _failed(gate_out):
    try:
        return [n for n, g in json.loads(gate_out)["gates"].items() if not g["ok"]]
    except Exception:
        return ["unknown"]


def execute(tid, message):
    """Run one task. FLOW_GATE=1: gate after each attempt, retry with the gate output (FLOW_RETRIES) inside one budget.
    API trouble (503, 429, timeouts) is retried with backoff and falls over to the next FLOW_BACKENDS entry."""
    workdir = os.environ.get("FLOW_WORKDIR", str(ROOT))
    budget, gated = float(os.environ.get("FLOW_MAX_USD", "0.50")), os.environ.get("FLOW_GATE") == "1"
    retries, model = int(os.environ.get("FLOW_RETRIES", "2")), os.environ.get("FLOW_MODEL", "haiku")
    chain, key, r_ = agents.chain_from_env(f"claude:{model}"), f"taskstate:{tid}", redis()

    def hset(**kv):
        r_.call("HSET", key, *[x for k, v in kv.items() for x in (k, v)])
        r_.call("EXPIRE", key, TTL)

    hset(status="running", started=now(), model=model)
    t0 = time.time()
    rec = {"event": "exec", "task_id": tid, "model": model}
    tok = dict(input_tokens=0, cache_read=0, cache_create=0, output_tokens=0)
    spent, attempt, ok, res, gate_out, first_fail, trace = 0.0, 0, False, agents.Result(False), "", "", []
    prompt = bridge.recall(message) + message
    while True:
        attempt += 1
        gate_failed = False
        o = agents.Opts(workdir, os.environ.get("FLOW_TOOLS", agents.READ_TOOLS), max(budget - spent, 0.01), int(os.environ.get("FLOW_TIMEOUT", "600")))
        res, cost, tokens = agents.call(chain, prompt, o, trace)
        spent += cost
        for k, v in tokens.items():
            tok[k] = tok.get(k, 0) + v
        ok = res.ok
        if ok and gated:
            g = gate.run_gates(workdir)
            gate_out = json.dumps(g, ensure_ascii=False)
            gate_failed, ok = not g["ok"], g["ok"]
        if ok or not gate_failed or attempt > retries or spent >= budget:
            break
        first_fail = first_fail or gate_out
        prompt = f"{message}\n\nYour previous attempt failed these automated checks. Fix them:\n{gate_out[:1500]}"

    result = (res.text or "")[:4000]
    rec.update(status="done" if ok else "failed", duration_ms=int((time.time() - t0) * 1000), cost_usd=round(spent, 6), attempts=attempt, **tok)
    if res.backend:
        rec["backend"] = res.backend
    if len(trace) > 1:  # any retry or fallback: keep the evidence
        rec["api_events"] = len(trace)
    if not res.ok:
        rec["error"] = res.error
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


def try_lock(path):
    """Hold an OS file lock for the process lifetime (released automatically on crash). None = someone else holds it."""
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _single_instance():
    return try_lock(ROOT / ".listener.lock")


def listener_running():
    lock = _single_instance()
    if lock:
        lock.close()
    return lock is None


_WARNED = set()


def _process(path, redis_):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = INCOMING / "failed"
    message = _read_task(path)
    if message:
        tid = None
        try:
            tid = path.stem if path.stem.isdigit() and redis_.call("EXISTS", f"task:{path.stem}") else submit(message)
            ok, rec = execute(tid, message)
            dest = INCOMING / ("processed" if ok else "failed")
            (LOGS / f"{path.stem}_{stamp}.log").write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"{'done' if ok else 'failed'} task {tid} ({rec['attempts']} attempt(s), ${rec['cost_usd']})", flush=True)
        except (RedisError, OSError) as e:
            if tid is None:  # Redis was down before the task was tried: keep it queued for the next poll instead of parking it as failed
                if path.name not in _WARNED:
                    _WARNED.add(path.name)
                    print(f"redis unavailable, {path.name} stays queued: {type(e).__name__}: {e}", flush=True)
                return
            print(f"error on {path.name}: {type(e).__name__}: {e}", flush=True)
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
    from flow_engine import plan  # late import: plan needs engine
    r = redis()
    try:
        while True:
            # ponytail: tasks run one at a time; add a worker pool if queue depth matters
            plan.resume_stale()  # a plan runner that died mid-run is restarted here
            for p in sorted(INCOMING.glob("*.txt"), key=lambda p: p.stat().st_mtime):
                if time.time() - p.stat().st_mtime > 1:  # still being written otherwise
                    _process(p, r)
            time.sleep(poll)
    except KeyboardInterrupt:
        return 0
