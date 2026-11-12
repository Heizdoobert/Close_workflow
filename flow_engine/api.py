"""API-mode backend: one HTTP completion per call, behind the same Result contract as the CLI backends.

A chain entry "api:<provider>/<model>" (e.g. "api:anthropic/claude-haiku", "api:openai/gpt-4o") lands here via agents.BACKENDS.
Keys come from the environment only (ANTHROPIC_API_KEY, OPENAI_API_KEY): they are never stored, logged or put in an error string.
FLOW_API_BASE_URL overrides the endpoint for both providers (fake server, vLLM, an OpenAI-compatible gateway).
No tool loop yet: a call that asks for write tools is refused; a read-only call is answered as a plain completion.
"""

import hashlib
import os
import subprocess
import threading
import time

from flow_engine import agents, apiclient, apiprice

DEFAULT_BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
WRITE_TOOLS = {"Edit", "Write", "Bash"}
BREAKER_LIMIT = 5  # consecutive failed requests before an endpoint is skipped
BREAKER_COOL = 60  # seconds before one probe request is let through

breaker = {}  # provider -> consecutive failed requests
breaker_at = {}  # provider -> time of the last failure, or of the probe let through after the cool-down
cache = {}  # sha256 key -> answer text
_lock = threading.Lock()


def _fail(msg, name, transient=False):
    return agents.Result(False, error=msg[:400], transient=transient, backend=name)


def git_env():
    """os.environ without GIT_CONFIG_*: plan._nomode sets them, and a COUNT without its KEY makes git refuse to run. HEAD does not depend on config."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}


def _cache_key(workdir, model, prompt):
    """sha256 of the worktree HEAD, the model and the prompt. None when there is no git HEAD: then nothing is cached."""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workdir, capture_output=True, text=True, timeout=10, env=git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if head.returncode != 0 or not head.stdout.strip():
        return None
    return hashlib.sha256("\0".join((head.stdout.strip(), model, prompt)).encode("utf-8")).hexdigest()


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


def _breaker_ok(provider):
    with _lock:
        breaker.pop(provider, None)
        breaker_at.pop(provider, None)


def run_api(model, prompt, o):
    provider, _, mid = (model or "").partition("/")
    name = f"api:{model}"
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
    if ckey and ckey in cache:
        return agents.Result(True, cache[ckey], 0.0, {"cache_hit": 1}, backend=name)
    if not _breaker_allows(provider):
        return _fail("breaker open", name, transient=True)
    base = os.environ.get("FLOW_API_BASE_URL") or DEFAULT_BASE[provider]
    send = apiclient.anthropic_call if provider == "anthropic" else apiclient.openai_call
    max_tokens = int(os.environ.get("FLOW_API_MAX_TOKENS", "4096"))
    try:
        out = send(base, key, mid, "", prompt, max_tokens, o.timeout)
    except apiclient.ApiError as e:
        _breaker_failed(provider)
        return _fail(str(e), name, transient=e.transient)
    except (KeyError, IndexError, TypeError, AttributeError):
        return _fail("malformed API response", name)
    _breaker_ok(provider)
    tokens = dict(out["usage"])
    cost = apiprice.cost_of(model, tokens)
    if cost is None:
        cost = 0.0
        tokens["cost_unknown"] = 1  # flagged in the ledger: an unpriced call is never a silent zero
    if ckey:
        cache[ckey] = out["text"]
    return agents.Result(True, out["text"], cost, tokens, backend=name)
