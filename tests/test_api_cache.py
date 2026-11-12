"""API cache key (worktree path, HEAD, uncommitted state) and the Anthropic cache_control threshold. No real key, no network beyond the fake server."""

import json
import subprocess

import pytest

import fake_api
from flow_engine import api, apiclient

KEY = "sk-test-SECRET-do-not-leak"
FIXED_DATE = "2026-11-12T10:00:00+0000"


@pytest.fixture
def server(monkeypatch):
    srv = fake_api.start()
    monkeypatch.setenv("FLOW_API_BASE_URL", srv.base)
    monkeypatch.delenv("FLOW_API_CACHE_MIN_CHARS", raising=False)
    yield srv
    srv.shutdown()
    srv.server_close()


def git(path, *a):
    env = {**api.git_env(), "GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE}
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True, text=True, env=env).stdout


def git_repo(path):
    path.mkdir()
    git(path, "init", "-q")
    (path / "a.txt").write_text("one\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "base")
    return path


def test_same_head_in_two_directories_gives_different_keys(tmp_path):
    first = git_repo(tmp_path / "wt1")
    second = git_repo(tmp_path / "wt2")
    assert git(first, "rev-parse", "HEAD") == git(second, "rev-parse", "HEAD")  # the premise: identical HEAD
    key_first = api._cache_key(str(first), "m", "ping")
    key_second = api._cache_key(str(second), "m", "ping")
    assert key_first and key_second
    assert key_first != key_second


def test_editing_a_tracked_file_changes_the_key(tmp_path):
    repo = git_repo(tmp_path / "wt")
    before = api._cache_key(str(repo), "m", "ping")
    (repo / "a.txt").write_text("two\n")
    after = api._cache_key(str(repo), "m", "ping")
    assert before and after
    assert before != after


def test_no_key_without_git_head(tmp_path):
    assert api._cache_key(str(tmp_path), "m", "ping") is None


def test_long_prompt_sends_content_list_with_cache_control(server):
    prompt = "x" * 5000
    out = apiclient.anthropic_call(server.base, KEY, "claude-haiku-5-5", "", prompt, 64, 5)
    assert out["text"] == "hello world"
    msg = server.last_body["messages"][0]
    assert msg["content"] == [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]
    assert "system" not in server.last_body


def test_short_prompt_sends_plain_string_without_cache_control(server):
    prompt = "y" * 100
    apiclient.anthropic_call(server.base, KEY, "claude-haiku-5-5", "", prompt, 64, 5)
    assert server.last_body["messages"] == [{"role": "user", "content": prompt}]
    assert "cache_control" not in json.dumps(server.last_body)


def test_threshold_follows_flow_api_cache_min_chars(server, monkeypatch):
    monkeypatch.setenv("FLOW_API_CACHE_MIN_CHARS", "50")
    apiclient.anthropic_call(server.base, KEY, "claude-haiku-5-5", "", "z" * 100, 64, 5)
    assert isinstance(server.last_body["messages"][0]["content"], list)
    assert server.last_body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
