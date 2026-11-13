"""Token budget and token totals: plan.billable, the plan-level token budget, the per-call token cap in api.run_api, and the report section.

The plan tests use the fake-agent fixture of test_plan.py (POSIX shell launchers); the API tests use the fake server of test_api_backend.py. No real key."""

import sys

import pytest

from flow_engine import agents, api, plan
from test_api_backend import server  # the shared fake API server fixture
from test_plan import env, step  # the shared fake-agent fixture

ANTHROPIC_MODEL = "anthropic/claude-haiku"
REFUSED = "invalid argument: token budget too small for this call"


def test_billable_counts_billed_kinds_and_cache_reads_only_on_flag(monkeypatch):
    t = {"input_tokens": 10, "cache_read": 100, "cache_create": 5, "output_tokens": 7, "thinking": 3}
    monkeypatch.delenv("FLOW_BUDGET_CACHE_READ", raising=False)
    assert plan.billable(t) == 25
    monkeypatch.setenv("FLOW_BUDGET_CACHE_READ", "0")
    assert plan.billable(t) == 25
    monkeypatch.setenv("FLOW_BUDGET_CACHE_READ", "1")
    assert plan.billable(t) == 125
    assert plan.billable({}) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
def test_no_token_budget_by_default_reports_none_for_budget_and_left(env, monkeypatch):
    monkeypatch.delenv("FLOW_PLAN_MAX_TOKENS", raising=False)
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("no token budget")
    tok = plan.status(pid)["tokens"]
    assert tok["budget"] is None and tok["left"] is None and tok["billable"] > 0  # the planner call is counted anyway


@pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
def test_step_after_the_token_budget_is_spent_fails_with_token_message(env, monkeypatch):
    env.script([step("s1", files=["a.py"], acc="test -f a.py"), step("s2", deps=["s1"], files=["b.py"], acc="test -f b.py")],
               {"s1": {"files": {"a.py": "A = 1\n"}}, "s2": {"files": {"b.py": "B = 2\n"}}})
    pid = plan.create("two steps on a token budget")
    budget = plan.status(pid)["tokens"]["billable"] + 1  # one token of room: s1 spends more than that
    monkeypatch.setenv("FLOW_PLAN_MAX_TOKENS", str(budget))
    plan.approve(pid, wait_for=True)
    steps = plan.load(pid)[2]["steps"]
    assert steps["s1"]["status"] == "done"
    assert steps["s2"]["status"] == "failed" and steps["s2"]["error"] == "plan token budget used up"
    tok = plan.status(pid)["tokens"]
    assert tok["budget"] == budget and tok["left"] < 0


def test_api_call_under_a_token_cap_too_small_is_refused_before_any_request(server, tmp_path):
    o = agents.Opts(str(tmp_path), prompt_only=True, token_cap=300)
    res = api.run_api(ANTHROPIC_MODEL, "x" * 600, o)  # 600 chars ~ 200 input tokens: 100 left, under the 256 floor
    assert not res.ok and res.error == REFUSED and not res.transient
    assert server.last_path is None


def test_api_max_tokens_is_capped_to_what_the_budget_leaves(server, tmp_path):
    res = api.run_api(ANTHROPIC_MODEL, "x" * 6000, agents.Opts(str(tmp_path), prompt_only=True, token_cap=5000))
    assert res.ok
    assert server.last_body["max_tokens"] == min(4096, 5000 - 2000)  # 6000 chars ~ 2000 input tokens: the cap, not the 4096 default, binds
    res = api.run_api(ANTHROPIC_MODEL, "x" * 600, agents.Opts(str(tmp_path), prompt_only=True, token_cap=5000))
    assert res.ok
    assert server.last_body["max_tokens"] == min(4096, 5000 - 200)


@pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
def test_report_has_token_totals_and_budget_line(env, monkeypatch):
    monkeypatch.setenv("FLOW_PLAN_MAX_TOKENS", "100000")
    env.script([step("s1", files=["a.py"], acc="test -f a.py")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("report totals")
    assert plan.approve(pid, wait_for=True) == "done"
    rep = (env.home / "plans" / pid / "report.md").read_text(encoding="utf-8")
    assert "## Token tổng" in rep
    assert "ngân sách: 100000 token" in rep
    tok = plan.status(pid)["tokens"]
    assert tok["billable"] > 0 and tok["left"] == 100000 - tok["billable"]
