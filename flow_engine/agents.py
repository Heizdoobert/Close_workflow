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
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# matched against error text only, never against a successful answer
TRANSIENT = re.compile(
    r"\b(429|500|502|503|504|529)\b|overload|rate.?limit|capacity|temporar|time[sd]?[ -]?out|ECONNRESET|ECONNREFUSED|"
    r"connection (reset|closed|error|refused)|try again|unavailable|high demand|stream (closed|ended)", re.I)

READ_TOOLS = "Read,Grep,Glob"
WRITE_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
BASH_ALLOW = os.environ.get("FLOW_BASH_ALLOW", "Bash(python3:*) Bash(python:*) Bash(pytest:*) Bash(git status:*) Bash(git diff:*) Bash(ls:*) Bash(cat:*)")


# errors no retry can fix: fail loudly instead of looping forever
PERMANENT = re.compile(r"\b(401|403)\b|api key|unauthori[sz]ed|forbidden|expired|invalid (model|argument)|(model|command) .{0,40}not found|unknown model|unsupported model|billing", re.I)
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


def claude_bin():
    if os.environ.get("FLOW_CLAUDE"):
        return os.environ["FLOW_CLAUDE"]
    real = Path.home() / ".local" / "bin" / "claude.real"  # host binary; plain `claude` may be a Docker wrapper
    return str(real) if real.exists() else shutil.which("claude")


def _fail(msg, backend, force_transient=None):
    msg = (msg or "unknown error").strip()[:400]
    return Result(False, error=msg, transient=TRANSIENT.search(msg) is not None if force_transient is None else force_transient, backend=backend)


def _run(cmd, o, stdin=None, env=None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=o.timeout, cwd=o.workdir, env=env)


def run_claude(model, prompt, o):
    exe = claude_bin()
    if not exe:
        return _fail("claude CLI not found (install it or set FLOW_CLAUDE)", f"claude:{model}", False)
    # ponytail: --setting-sources project + strict MCP + no slash commands cuts a ~125k-token session preamble to ~7k
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--no-session-persistence", "--max-budget-usd", f"{o.budget:.2f}",
           "--setting-sources", "project", "--strict-mcp-config", "--disable-slash-commands", "--tools", o.tools]
    if "Edit" in o.tools or "Write" in o.tools or "Bash" in o.tools:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", BASH_ALLOW]
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


def policy_chain(parent_model):
    """Sub-agent model policy from the model of the agent that handed us the work (its own id, e.g. from `agy models`).
    GPT-OSS 120B parent  -> inherit it, pinned: no other model, ever.
    Gemini Flash parent  -> prefer GPT-OSS 120B with persistent retries; only if it truly cannot be called, the lighter models."""
    m = (parent_model or "").lower()
    if re.search(r"gpt-?oss.*120b", m):
        return [f"agy:{parent_model}!"]
    if "gemini" in m and "flash" in m:
        return [f"agy:{GPT_OSS}+", f"agy:{parent_model}", "agy:gemini-3.8-flash-low"]
    return []


def chain_from_env(default="claude:haiku", parent_model=None):
    if os.environ.get("FLOW_BACKENDS"):  # an explicit chain always wins
        return [s.strip() for s in os.environ["FLOW_BACKENDS"].split(",") if s.strip()]
    chain = policy_chain(parent_model or os.environ.get("FLOW_PARENT_MODEL"))
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
                    oo = Opts(o.workdir, o.tools, o.budget, o.timeout, res.session)
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
