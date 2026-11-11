"""Sub-agent backends (claude, agy, opencode) behind one call with retry, backoff and fallback.

A backend spec is "claude:haiku", "claude:sonnet", "agy[:model-id]" or "opencode[:provider/model]".
Suffix "!" pins the model: persistent retries, resume of the same conversation, and NEVER a fallback to another model.
Suffix "+" prefers the model with persistent retries, then (only if it truly cannot be called) the next spec runs.
Transient failures (503/529/429/timeouts/connection resets) are retried with exponential backoff;
a hard failure (auth, bad request, exhausted retries) falls through to the next backend in the chain.
Every attempt is appended to `trace` so the final report can prove what happened.
"""

import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from flow_engine import sandbox

# matched against error text only, never against a successful answer
TRANSIENT = re.compile(
    r"\b(429|500|502|503|504|529)\b|overload|rate.?limit|capacity|temporar|time[sd]?[ -]?out|ECONNRESET|ECONNREFUSED|"
    r"connection (reset|closed|error|refused)|try again|unavailable|high demand|stream (closed|ended)", re.I)

READ_TOOLS = "Read,Grep,Glob"
WRITE_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
BASH_ALLOW = os.environ.get("FLOW_BASH_ALLOW", "Bash(python3:*) Bash(python:*) Bash(pytest:*) Bash(git status:*) Bash(git diff:*) Bash(ls:*) Bash(cat:*)")


# errors no retry can fix: fail loudly instead of looping forever
PERMANENT = re.compile(r"\b(401|403)\b|api key|unauthori[sz]ed|forbidden|expired|invalid (model|argument)|(model|command) .{0,40}not found|unknown model|unsupported model|billing|"
                       r"OCI runtime|no such file or directory|exec failed|permission denied|docker daemon|not found in \$PATH", re.I)
GPT_OSS = "gpt-oss-120b-medium"  # the model id `agy models` lists for GPT-OSS 120B


@dataclass
class Result:
    ok: bool
    text: str = ""
    cost: float = 0.0
    tokens: dict = field(default_factory=dict)
    error: str = ""
    transient: bool = False
    backend: str = ""
    session: str = ""  # agy conversation id, lets a retry continue instead of restart


@dataclass
class Opts:
    workdir: str = "."
    tools: str = READ_TOOLS
    budget: float = 0.50
    timeout: int = 600
    resume: str = ""
    on_group: object = None  # called with the agent's process group id right after it starts (plan.py records it for recover)


def claude_bin():
    if os.environ.get("FLOW_CLAUDE"):
        return os.environ["FLOW_CLAUDE"]
    real = Path.home() / ".local" / "bin" / "claude.real"  # host binary; plain `claude` may be a Docker wrapper
    return str(real) if real.exists() else shutil.which("claude")


def _fail(msg, backend, force_transient=None):
    msg = (msg or "unknown error").strip()[:400]
    return Result(False, error=msg, transient=TRANSIENT.search(msg) is not None if force_transient is None else force_transient, backend=backend)


# Global cap on concurrent agent subprocesses (FLOW_MAX_AGENTS, default 4, minimum 1).
# Two layers: a threading.BoundedSemaphore for threads in this process, and one OS file lock per slot
# (slot-0..N-1 under FLOW_SLOT_DIR, default <tempdir>/flow-agent-slots) so separate plan runners share the cap.
# OS locks are released by the kernel when a process dies, so a killed runner never leaks a slot.
_sem, _sem_n = None, 0
_sem_lock = threading.Lock()


def max_agents():
    try:
        n = int(os.environ.get("FLOW_MAX_AGENTS", "4"))
    except ValueError:
        n = 4
    return max(1, n)


def _acquire_file_slot(n):
    d = Path(os.environ.get("FLOW_SLOT_DIR") or Path(tempfile.gettempdir()) / "flow-agent-slots")
    d.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(n):
            f = open(d / f"slot-{i}.lock", "a+")
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                f.close()
        time.sleep(0.05)


def _release_file_slot(f):
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


@contextmanager
def _slot():
    global _sem, _sem_n
    n = max_agents()
    with _sem_lock:
        if _sem is None or _sem_n != n:
            _sem, _sem_n = threading.BoundedSemaphore(n), n
        sem = _sem
    sem.acquire()
    try:
        f = _acquire_file_slot(n)
        try:
            yield
        finally:
            _release_file_slot(f)
    finally:
        sem.release()


def new_group():
    """Popen kwargs that put the child in its own process group, so a timeout can kill its children too."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# an agent's command line names claude or agy (also the bwrap-wrapped and claude.real forms); a stray "fake_claude.py" does not count
AGENT_CMD = re.compile(r"(?:^|[\s/])(?:claude|agy)(?:\.real)?(?:\s|$)")


def _load_libc():
    """libc for prctl, loaded once at import: the fork child must not import ctypes (an import lock held by another thread can deadlock it)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True)
    except Exception:
        return None


_LIBC = _load_libc()


def _die_with_runner():
    """Linux, runs in the child before exec: PR_SET_PDEATHSIG makes the kernel SIGKILL the agent when the thread that forked it exits.
    That thread is a step thread of the plan runner, alive as long as the runner, so this fires only when the runner dies (SIGKILL, crash).
    Note: PDEATHSIG is tied to the forking thread, not the process. Step threads live as long as the runner's pool, so this is acceptable.
    Any failure leaves the old behaviour: no-op."""
    try:
        _LIBC.prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG = 1
    except Exception:
        pass


def _group_cmdlines(pgid):
    """Command lines of the live processes in process group pgid. Group members, not just the leader: once the leader dies, its children stay in the group."""
    if sys.platform.startswith("linux"):
        out = []
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            try:
                stat = (d / "stat").read_text(errors="replace")
                if int(stat.rsplit(")", 1)[1].split()[2]) == pgid:  # field 5 (pgrp), after the "(comm)" which may hold spaces
                    out.append((d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"))
            except (OSError, ValueError, IndexError):
                continue  # the process exited while we looked
        return out
    if os.name == "nt":
        return []
    p = subprocess.run(["ps", "-axo", "pgid=,args="], capture_output=True, text=True, errors="replace")
    return [line.split(None, 1)[1] for line in p.stdout.splitlines() if line.split()[:1] == [str(pgid)] and len(line.split(None, 1)) == 2]


def kill_orphan_group(pgid):
    """SIGKILL an agent process group a dead runner left behind. Only if it still holds an agent: the pgid may have been reused. True if killed."""
    if os.name == "nt" or pgid <= 1 or pgid == os.getpgrp():
        return False
    if not any(AGENT_CMD.search(c) for c in _group_cmdlines(pgid)):
        return False
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        return False
    return True


def kill_group(p):
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)  # session leader: its process group id is its pid
        except OSError:
            pass  # already gone
    p.kill()
    p.wait()


def _run(cmd, o, stdin=None, env=None):
    with _slot():  # only the subprocess itself holds a slot: backoff sleeps happen outside
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                             cwd=o.workdir, env=env, preexec_fn=_die_with_runner if sys.platform.startswith("linux") else None, **new_group())
        if o.on_group:
            o.on_group(p.pid)  # session leader: its pid is the process group id
        try:
            out, err = p.communicate(stdin, timeout=o.timeout)
        except subprocess.TimeoutExpired:
            kill_group(p)  # subprocess.run would kill only the direct child and leave its children running
            raise
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def run_claude(model, prompt, o):
    exe = claude_bin()
    if not exe:
        return _fail("claude CLI not found (install it or set FLOW_CLAUDE)", f"claude:{model}", False)
    # ponytail: --setting-sources project + strict MCP + no slash commands cuts a ~125k-token session preamble to ~7k
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--max-budget-usd", f"{o.budget:.2f}",
           "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands", "--tools", o.tools]
    if "Edit" in o.tools or "Write" in o.tools or "Bash" in o.tools:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", BASH_ALLOW]
    if sandbox.available() == "bwrap":
        cmd = sandbox.wrap(cmd, o.workdir)
    else:
        sandbox.warn_no_sandbox()
    try:
        p = _run(cmd, o, stdin=prompt)
    except subprocess.TimeoutExpired:
        return _fail(f"timeout after {o.timeout}s", f"claude:{model}", True)
    except OSError as e:
        return _fail(str(e), f"claude:{model}", False)
    try:
        out = json.loads(p.stdout)
        r = [e for e in out if e.get("type") == "result"][-1] if isinstance(out, list) else out
    except Exception:
        return _fail(p.stderr or p.stdout or f"exit {p.returncode}", f"claude:{model}")
    u = r.get("usage", {})
    tokens = dict(input_tokens=u.get("input_tokens", 0), cache_read=u.get("cache_read_input_tokens", 0),
                  cache_create=u.get("cache_creation_input_tokens", 0), output_tokens=u.get("output_tokens", 0))
    if p.returncode != 0 or r.get("is_error"):
        res = _fail(r.get("result") or p.stderr or f"exit {p.returncode}", f"claude:{model}")
        res.cost, res.tokens = r.get("total_cost_usd", 0), tokens
        return res
    return Result(True, r.get("result") or "", r.get("total_cost_usd", 0), tokens, backend=f"claude:{model}")


def run_agy(model, prompt, o):
    exe = os.environ.get("FLOW_AGY") or shutil.which("agy")
    model = model or GPT_OSS
    name = f"agy:{model}"
    if not exe:
        return _fail("agy CLI not found", name, False)
    # --model is always explicit: never rely on a default that a wrapper or settings change could swap
    cmd = [exe, "-p", prompt, "--model", model, "--output-format", "json", "--print-timeout", f"{o.timeout}s"]
    if o.resume:
        cmd += ["--conversation", o.resume]
    if "Edit" in o.tools or "Write" in o.tools or "Bash" in o.tools:
        cmd += ["--mode", "accept-edits"]
    try:
        p = _run(cmd, o)
    except subprocess.TimeoutExpired:
        return _fail(f"timeout after {o.timeout}s", name, True)
    except OSError as e:
        return _fail(str(e), name, False)
    try:
        r = json.loads(p.stdout)
    except Exception:
        return _fail(p.stderr or p.stdout or f"exit {p.returncode}", name)
    u = r.get("usage", {})
    tokens = dict(input_tokens=u.get("input_tokens", 0), cache_read=u.get("cache_read_tokens", 0), cache_create=0, output_tokens=u.get("output_tokens", 0))
    sid = r.get("conversation_id", "") or o.resume
    if r.get("status") != "SUCCESS":
        res = _fail(f"{r.get('status')}: {r.get('response', '')}", name)
        res.session, res.tokens = sid, tokens
        return res
    return Result(True, r.get("response", ""), 0.0, tokens, backend=name, session=sid)  # agy reports tokens, not dollars


def run_opencode(model, prompt, o):
    exe = os.environ.get("FLOW_OPENCODE") or shutil.which("opencode")
    if not exe:
        return _fail("opencode CLI not found", "opencode", False)
    cmd = [exe, "run", prompt, "--format", "json", "--dir", o.workdir] + (["-m", model] if model else [])
    try:
        p = _run(cmd, o, env={**os.environ, "NO_SANDBOX": "1"})  # NO_SANDBOX: host binary, not the Docker wrapper
    except subprocess.TimeoutExpired:
        return _fail(f"timeout after {o.timeout}s", "opencode", True)
    except OSError as e:
        return _fail(str(e), "opencode", False)
    texts, err, cost, tok = [], None, 0.0, dict(input_tokens=0, cache_read=0, cache_create=0, output_tokens=0)
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "error":
            d = (ev.get("error") or {}).get("data") or {}
            err = f"{d.get('statusCode', '')} {d.get('message') or ev.get('error')}"
            if d.get("isRetryable") is False and not TRANSIENT.search(str(d.get("statusCode", ""))):
                return _fail(err, "opencode", False)
        elif ev.get("type") == "text":
            texts.append((ev.get("part") or {}).get("text") or ev.get("text") or "")
        elif ev.get("type") == "step_finish":
            pt = ev.get("part") or {}
            cost += pt.get("cost", 0) or 0
            t = pt.get("tokens") or {}
            tok["input_tokens"] += t.get("input", 0)
            tok["output_tokens"] += t.get("output", 0)
    if err:
        return _fail(err, "opencode")
    if p.returncode != 0 or not texts:
        return _fail(p.stderr or "no text in opencode output", "opencode")
    return Result(True, "".join(texts).strip(), cost, tok, backend="opencode")


BACKENDS = {"claude": run_claude, "agy": run_agy, "opencode": run_opencode}


def policy_chain(parent_model, role="code"):
    """Sub-agent model policy from the model of the agent that handed us the work (its own id, e.g. from `agy models`).
    GPT-OSS 120B parent  -> inherit it, pinned: no other model, ever.
    Gemini Flash parent  -> role "code" (steps that write code, quick work): prefer GPT-OSS 120B with persistent retries; only if it truly cannot be called, Gemini.
                            role "general" (planner, non-code steps, review, SHIP): Gemini itself, then the lighter one."""
    m = (parent_model or "").lower()
    if re.search(r"gpt-?oss.*120b", m):
        return [f"agy:{parent_model}!"]
    if "gemini" in m and "flash" in m:
        return [f"agy:{GPT_OSS}+"] * (role == "code") + [f"agy:{parent_model}", "agy:gemini-3.8-flash-low"]
    return []


def chain_from_env(default="claude:haiku", parent_model=None, role="code"):
    if os.environ.get("FLOW_BACKENDS"):  # an explicit chain always wins
        return [s.strip() for s in os.environ["FLOW_BACKENDS"].split(",") if s.strip()]
    chain = policy_chain(parent_model or os.environ.get("FLOW_PARENT_MODEL"), role)
    out = []
    for s in chain or [default]:
        if s not in out:
            out.append(s)
    return out


def call(chain, prompt, o, trace=None, sleep=time.sleep):
    """Run the prompt on the first backend that succeeds. Returns (Result, total_cost, total_tokens).
    Pinned ("!") specs retry persistently and never fall back; preferred ("+") specs retry persistently, then fall back."""
    retries = int(os.environ.get("FLOW_API_RETRIES", "3"))
    persist = int(os.environ.get("FLOW_PIN_RETRIES", "10"))
    base, cap = float(os.environ.get("FLOW_BACKOFF_BASE", "2")), float(os.environ.get("FLOW_BACKOFF_CAP", "30"))
    pin_cap = float(os.environ.get("FLOW_PIN_BACKOFF_CAP", "60"))
    trace = trace if trace is not None else []
    spent, tokens, last = 0.0, {}, Result(False, error="no backend configured")
    for i, raw in enumerate(chain):
        pinned, prefer = raw.endswith("!"), raw.endswith("+")
        spec = raw.rstrip("!+")
        name, _, model = spec.partition(":")
        fn = BACKENDS.get(name)
        if not fn:
            trace.append({"t": time.time(), "backend": spec, "ok": False, "error": "unknown backend", "action": "skip"})
            continue
        n, limit, ceiling = retries, retries, cap
        if pinned or prefer:
            n, limit, ceiling = persist, persist, pin_cap
        oo, prompt_now = o, prompt
        for attempt in range(n + 1):
            res = fn(model or ("haiku" if name == "claude" else None), prompt_now, oo)
            spent += res.cost
            for k, v in res.tokens.items():
                tokens[k] = tokens.get(k, 0) + v
            ev = {"t": time.time(), "backend": spec, "attempt": attempt + 1, "ok": res.ok, "error": res.error[:200], "transient": res.transient}
            last = res
            if res.ok:
                trace.append({**ev, "action": "done"})
                return res, spent, tokens
            permanent = PERMANENT.search(res.error) is not None and not res.transient
            # pinned/preferred: any error that a retry could cure is retried; others only when transient
            retryable = (res.transient or (pinned or prefer)) and not permanent
            if retryable and attempt < limit:
                delay = min(ceiling, base * 2 ** attempt) * (0.5 + random.random() / 2)
                if res.session:  # agy: continue the same conversation instead of starting over
                    oo = replace(o, resume=res.session)
                    prompt_now = "Your previous turn was interrupted by an error. Continue exactly where you stopped and finish the task."
                trace.append({**ev, "action": f"retry in {delay:.1f}s" + (" (same conversation)" if res.session else "")})
                sleep(delay)
                continue
            if pinned:
                trace.append({**ev, "action": "pinned model: no fallback to another model" + (" (permanent error)" if permanent else " (retries exhausted)")})
                return last, spent, tokens
            trace.append({**ev, "action": "fallback to next backend" if i < len(chain) - 1 else "give up"})
            break
    return last, spent, tokens
