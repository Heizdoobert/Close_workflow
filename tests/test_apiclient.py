"""API wire client against the fake server: parsing, usage mapping, error classes, no key in errors."""

import contextlib
import http.server
import socket
import threading

import pytest

import fake_api
from flow_engine import apiclient

KEY = "sk-test-SECRET-do-not-leak"


@pytest.fixture
def server():
    srv = fake_api.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@contextlib.contextmanager
def _local_http(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_anthropic_call_parses_text_usage_and_sends_headers(server):
    out = apiclient.anthropic_call(server.base, KEY, "claude-haiku-5-5", "be brief", "ping", 64, 5)
    assert out["text"] == "hello world"
    assert out["usage"] == {"input_tokens": 12, "cache_read": 4, "cache_create": 3, "output_tokens": 5, "thinking": 0}
    assert server.last_path == "/v1/messages"
    assert server.last_headers["x-api-key"] == KEY
    assert server.last_headers["anthropic-version"] == "2023-06-01"
    assert server.last_body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert server.last_body["messages"] == [{"role": "user", "content": "ping"}]


def test_anthropic_call_omits_empty_system(server):
    apiclient.anthropic_call(server.base, KEY, "claude-haiku-5-5", "", "ping", 64, 5)
    assert "system" not in server.last_body


def test_openai_call_parses_text_and_splits_cached_prompt_tokens(server):
    out = apiclient.openai_call(server.base, KEY, "gpt-oss-120b", "be brief", "ping", 64, 5)
    assert out["text"] == "hi there"
    assert out["usage"] == {"input_tokens": 12, "cache_read": 8, "cache_create": 0, "output_tokens": 6, "thinking": 0}
    assert server.last_path == "/v1/chat/completions"
    assert server.last_headers["authorization"] == f"Bearer {KEY}"
    assert server.last_body["messages"][0] == {"role": "system", "content": "be brief"}


def test_429_is_transient_and_keeps_retry_after(server):
    server.fail_next = 429
    server.retry_after = "2"
    with pytest.raises(apiclient.ApiError) as info:
        apiclient.anthropic_call(server.base, KEY, "m", "", "ping", 64, 5)
    assert info.value.status == 429
    assert info.value.transient is True
    assert info.value.retry_after == 2.0


def test_500_is_transient(server):
    server.fail_next = 500
    with pytest.raises(apiclient.ApiError) as info:
        apiclient.openai_call(server.base, KEY, "m", "", "ping", 64, 5)
    assert info.value.status == 500
    assert info.value.transient is True
    assert info.value.retry_after is None


def test_400_is_not_transient(server):
    server.fail_next = 400
    with pytest.raises(apiclient.ApiError) as info:
        apiclient.anthropic_call(server.base, KEY, "m", "", "ping", 64, 5)
    assert info.value.status == 400
    assert info.value.transient is False


def test_error_message_never_contains_key_or_body(server):
    server.fail_next = 401
    with pytest.raises(apiclient.ApiError) as info:
        apiclient.openai_call(server.base, KEY, "m", "", "ping", 64, 5)
    assert str(info.value) == "HTTP 401 Unauthorized"
    assert KEY not in str(info.value)


def test_unreachable_base_is_transient_with_class_name_only():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(apiclient.ApiError) as info:
        apiclient.anthropic_call(f"http://127.0.0.1:{port}", KEY, "m", "", "ping", 64, 2)
    assert info.value.transient is True
    assert info.value.status is None
    assert str(info.value) in ("URLError", "ConnectionRefusedError")
    assert KEY not in str(info.value)


class _Target(http.server.BaseHTTPRequestHandler):
    hits = []

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        _Target.hits.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()


def test_redirect_is_refused_and_key_never_reaches_target():
    _Target.hits = []
    with _local_http(_Target) as target_base:

        class Redirector(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                self.rfile.read(length)
                self.send_response(302)
                self.send_header("Location", target_base + "/v1/messages")
                self.send_header("content-length", "0")
                self.end_headers()

        with _local_http(Redirector) as redirect_base:
            with pytest.raises(apiclient.ApiError) as info:
                apiclient.request(redirect_base + "/v1/messages", {"x-api-key": KEY}, {"a": 1}, 5)
    assert info.value.status == 302
    assert info.value.transient is False
    assert _Target.hits == []
    assert KEY not in str(info.value)


def test_plain_http_to_remote_host_is_refused_before_any_socket(monkeypatch):
    opened = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: opened.append(a))
    with pytest.raises(apiclient.ApiError) as info:
        apiclient.request("http://example.com/v1/messages", {"x-api-key": KEY}, {"a": 1}, 5)
    assert str(info.value) == "invalid argument: api base url must be https"
    assert info.value.transient is False
    assert opened == []


def test_body_over_max_bytes_is_permanent(monkeypatch):
    monkeypatch.setenv("FLOW_API_MAX_BYTES", "100")

    class Big(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-length", "200")
            self.end_headers()
            self.wfile.write(b"x" * 200)

    with _local_http(Big) as base:
        with pytest.raises(apiclient.ApiError) as info:
            apiclient.request(base + "/v1/messages", {"x-api-key": KEY}, {"a": 1}, 5)
    assert str(info.value) == "invalid argument: response over FLOW_API_MAX_BYTES"
    assert info.value.transient is False


def test_truncated_body_is_transient_incomplete_read():
    class Truncated(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("content-length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-length", "1000")
            self.end_headers()
            self.wfile.write(b'{"a":')
            self.wfile.flush()
            self.close_connection = True

    with _local_http(Truncated) as base:
        with pytest.raises(apiclient.ApiError) as info:
            apiclient.request(base + "/v1/messages", {"x-api-key": KEY}, {"a": 1}, 5)
    assert str(info.value) == "IncompleteRead"
    assert info.value.transient is True
