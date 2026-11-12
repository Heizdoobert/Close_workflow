"""API wire client against the fake server: parsing, usage mapping, error classes, no key in errors."""

import socket

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
