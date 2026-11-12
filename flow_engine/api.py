"""API-mode backend: one HTTP completion per call, behind the same Result contract as the CLI backends.

A chain entry "api:<provider>/<model>" (e.g. "api:anthropic/claude-haiku", "api:openai/gpt-4o") lands here via agents.BACKENDS.
Keys come from the environment only (ANTHROPIC_API_KEY, OPENAI_API_KEY): they are never stored, logged or put in an error string.
FLOW_API_BASE_URL overrides the endpoint for both providers (fake server, vLLM, an OpenAI-compatible gateway).
No tool loop yet: only calls with o.prompt_only are served (review, SHIP personas, interview round); any other call is refused.
A prompt-only call that asks for write tools is refused too.
"""

import hashlib
import math
import os
import subprocess
import threading
import time

from flow_engine import agents, apiclient, apilog, apiprice, engine

DEFAULT_BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
WRITE_TOOLS = {"Edit", "Write", "Bash"}
BREAKER_LIMIT = 5  # consecutive failed requests before an endpoint is skipped
BREAKER_COOL = 60  # seconds before one probe request is let through

breaker = {}  # provider -> consecutive failed requests
breaker_at = {}  # provider -> time of the last failure, or of the probe let through after the cool-down
cache = {}  # sha256 key -> answer text
_lock = threading.Lock()


def _fail(msg, name, transient=False, retry_after=0.0, tokens=None, budget=False):
    return agents.Result(False, error=msg[:400], transient=transient, backend=name, retry_after=retry_after, tokens=dict(tokens or {}), budget=budget)


def git_env():
    """os.environ without GIT_CONFIG_*: plan._nomode sets them, and a COUNT without its KEY makes git refuse to run. HEAD does not depend on config."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}


def _git_out(workdir, *args):
    """stdout of a git command run in workdir, or None when it fails."""
    try:
        res = subprocess.run(["git", *args], cwd=workdir, capture_output=True, text=True, errors="replace", timeout=10, env=git_env())  # a diff of a latin-1 file is not UTF-8
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def _cache_key(workdir, model, prompt):
    """sha256 of the worktree path, its HEAD, its uncommitted state, the model and the prompt. None when git cannot say: then nothing is cached."""
    head = _git_out(workdir, "rev-parse", "HEAD")
    if not head or not head.strip():
        return None
    status = _git_out(workdir, "status", "--porcelain")
    diff = _git_out(workdir, "diff", "HEAD")
    if status is None or diff is None:
        return None
    parts = (os.path.realpath(workdir), head.strip(), status, diff, model, prompt)
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _breaker_allows(provider):
    with _lock:
        if breaker.get(provider, 0) < BREAKER_LIMIT:
            return True
        if time.time() - breaker_at.get(provider, 0) < BREAKER_COOL:
            return False
        breaker_at[provider] = time.time()  # half-open: this request is the probe, the others keep waiting for it
        return True


def _breaker_failed(provider):
    with _lock:
        breaker[provider] = breaker.get(provider, 0) + 1
        breaker_at[provider] = time.time()


def _breaker_rearm(provider):
    """A permanent answer to the half-open probe: the count stays, the cool-down restarts from now (the probe may have taken longer than the cool-down)."""
    with _lock:
        if breaker.get(provider, 0) >= BREAKER_LIMIT:
            breaker_at[provider] = time.time()


def _breaker_ok(provider):
    with _lock:
        breaker.pop(provider, None)
        breaker_at.pop(provider, None)


def _log(o, provider, model, usage, cost, started, status):
    """One JSON line for this attempt (apilog.line): no prompt, no key."""
    latency_ms = (time.monotonic() - started) * 1000
    engine.log(apilog.line(getattr(o, "role", ""), f"api:{provider}", model, usage, cost, latency_ms, status))


def run_api(model, prompt, o):
    provider, _, mid = (model or "").partition("/")
    name = f"api:{model}"
    if not o.prompt_only:  # no tool loop: a role that reads the repo would get a plain completion with no repo access
        return _fail("invalid argument: api backend only serves prompt-only calls", name)
    if provider not in DEFAULT_BASE:
        return _fail(f"invalid argument: unknown provider {provider}", name)
    if not mid:
        return _fail("invalid argument: api spec must be <provider>/<model>", name)
    if WRITE_TOOLS & set(o.tools.split(",")):
        return _fail("invalid argument: api backend has no tool loop, write tools are not supported", name)
    key = os.environ.get(KEY_ENV[provider], "")
    if not key:
        return _fail(f"missing api key for {provider}", name)
    try:
        apiprice.price_for(model)  # a malformed FLOW_PRICE_* is a config error: fail before any request is sent
    except ValueError as e:
        return _fail(f"invalid argument: {e}", name)
    # ponytail: in-process only, and replaying a stored answer is safe only for an identical prompt on an identical tree (HEAD is the tree here)
    ckey = _cache_key(o.workdir, model, prompt) if os.environ.get("FLOW_API_CACHE") == "1" else None
    started = time.monotonic()
    with _lock:
        hit = cache.get(ckey) if ckey else None
    if hit is not None:
        _log(o, provider, model, {}, 0.0, started, "cache_hit")
        return agents.Result(True, hit, 0.0, {"cache_hit": 1}, backend=name)
    if not _breaker_allows(provider):
        return _fail("breaker open", name, transient=True)
    max_tokens = int(os.environ.get("FLOW_API_MAX_TOKENS", "4096"))
    token_cap = getattr(o, "token_cap", 0)
    if token_cap > 0:  # the plan's token budget left for this call: the estimated input plus max_tokens must fit in it
        est_in = math.ceil(len(prompt) / float(os.environ.get("FLOW_TOKEN_CHARS") or 3))
        max_tokens = min(max_tokens, token_cap - est_in)
        if max_tokens < 256:
            return _fail("invalid argument: token budget too small for this call", name, budget=True)
    base = os.environ.get("FLOW_API_BASE_URL") or DEFAULT_BASE[provider]
    send = apiclient.anthropic_call if provider == "anthropic" else apiclient.openai_call
    try:
        out = send(base, key, mid, "", prompt, max_tokens, o.timeout)
    except apiclient.ApiError as e:
        if e.transient:  # a 400/401/404 says nothing about the endpoint's health: only transient failures open the breaker
            _breaker_failed(provider)  # also re-arms the cool-down when this was the half-open probe
        else:
            _breaker_rearm(provider)
        _log(o, provider, model, {}, 0.0, started, "transient" if e.transient else "error")
        return _fail(str(e), name, transient=e.transient, retry_after=float(e.retry_after or 0.0))
    except (KeyError, IndexError, TypeError, AttributeError):
        _log(o, provider, model, {}, 0.0, started, "malformed")
        return _fail("malformed API response", name)
    _breaker_ok(provider)
    if out["truncated"] or not out["text"].strip():
        _log(o, provider, model, out["usage"], 0.0, started, "truncated" if out["truncated"] else "empty")
        return _fail("empty or truncated answer", name, tokens=out["usage"])  # billed, so counted; not transient, not cached
    tokens = dict(out["usage"])
    priced = apiprice.cost_of(model, tokens)  # None without FLOW_PRICE_*: the log keeps that null; USD is optional, the plan budgets tokens
    _log(o, provider, model, tokens, priced, started, "ok")
    if ckey:
        with _lock:
            cache[ckey] = out["text"]
    return agents.Result(True, out["text"], priced or 0.0, tokens, backend=name)
