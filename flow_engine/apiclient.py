"""HTTP wire client for API-mode backends (stdlib only: urllib + json).

Keys are passed in by the caller and never stored here. ApiError messages carry only
the status and reason phrase or the exception class name, never the response body,
the request headers or the key, so no secret can leak through an error string.
"""

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request

ANTHROPIC_VERSION = "2023-06-01"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ApiError(Exception):
    def __init__(self, message, status=None, transient=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.transient = transient
        self.retry_after = retry_after


def _retry_after(headers):
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _scheme_allowed(url):
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)
    except ValueError:
        return False


def request(url, headers, body, timeout):
    if not _scheme_allowed(url):
        raise ApiError("invalid argument: api base url must be https", transient=False)
    limit = int(os.environ.get("FLOW_API_MAX_BYTES", "4000000"))
    data = json.dumps(body).encode("utf-8")
    all_headers = {**headers, "content-type": "application/json"}
    req = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(limit + 1)
            if len(raw) > limit:
                raise ApiError("invalid argument: response over FLOW_API_MAX_BYTES", transient=False)
            if resp.length:
                raise http.client.IncompleteRead(raw)
    except urllib.error.HTTPError as e:
        status, reason = e.code, e.reason
        retry_after = _retry_after(e.headers)
        e.close()
        transient = status == 429 or status >= 500
        raise ApiError(f"HTTP {status} {reason}", status=status, transient=transient, retry_after=retry_after) from None
    except (urllib.error.URLError, OSError) as e:
        raise ApiError(type(e).__name__, transient=True) from None
    except http.client.HTTPException as e:
        raise ApiError(type(e).__name__, transient=True) from None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ApiError("invalid JSON in response", transient=False) from None
    if not isinstance(parsed, dict):
        raise ApiError("response is not a JSON object", transient=False)
    return parsed


def anthropic_call(base, key, model, system, user, max_tokens, timeout):
    headers = {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    content = user
    if len(user) >= int(os.environ.get("FLOW_API_CACHE_MIN_CHARS", "4000")):  # cache_control only pays off on a long prompt
        content = [{"type": "text", "text": user, "cache_control": {"type": "ephemeral"}}]
    body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": content}]}
    if system:
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    resp = request(base.rstrip("/") + "/v1/messages", headers, body, timeout)
    text = "".join(b.get("text", "") for b in resp["content"] if b.get("type") == "text")
    u = resp.get("usage") or {}
    usage = {
        "input_tokens": u.get("input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "cache_create": u.get("cache_creation_input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "thinking": 0,
    }
    return {"text": text, "usage": usage, "truncated": resp.get("stop_reason") == "max_tokens"}


def openai_call(base, key, model, system, user, max_tokens, timeout):
    headers = {"Authorization": f"Bearer {key}"}
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    resp = request(base.rstrip("/") + "/v1/chat/completions", headers, body, timeout)
    choice = resp["choices"][0]
    text = choice["message"].get("content") or ""
    finish_reason = choice.get("finish_reason")
    u = resp.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    usage = {
        "input_tokens": u.get("prompt_tokens", 0) - cached,
        "cache_read": cached,
        "cache_create": 0,
        "output_tokens": u.get("completion_tokens", 0),
        "thinking": 0,
    }
    return {"text": text, "usage": usage, "truncated": finish_reason == "length"}
