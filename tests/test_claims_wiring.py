"""Claims check wired into the plan flow: analysis.md is written at create, run_claims runs once after integration, its blockers reach SHIP, and the report shows it."""

import json
import subprocess
from types import SimpleNamespace

import pytest

import test_plan
from flow_engine import claims, method, plan

env = test_plan.env
step = test_plan.step
ANALYSIS = {"research": [{"file": "README.md", "finding": "the repo only holds a readme"}], "root_cause": "nothing implements the goal yet",
            "approach": "one module per step", "alternatives": [{"option": "one big file", "rejected_because": "no parallel steps"}],
            "fix": "small modules, each checked by its own acceptance command", "blast_radius": {"affected": ["README.md"], "risks": "none"},
            "spec": "each step writes its file"}
OK = {"status": "ok", "hard": [], "soft": [], "n_hard": 0, "n_soft": 0}


def _git(tmp_path, *a):
    return subprocess.run(["git", *a], cwd=str(tmp_path), check=True, capture_output=True, text=True).stdout.strip()


def _ctx(tmp_path, monkeypatch, claimed="PASS", acc_ok=True, with_analysis=True, integration_ok=True):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "flow@localhost")
    _git(tmp_path, "config", "user.name", "flow-engine")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    sha = _git(tmp_path, "rev-parse", "--short", "HEAD")
    d = tmp_path / "plan"
    (d / "wt" / "_plan").mkdir(parents=True)
    (d / "wt" / "_plan" / "README.md").write_text("hello\n")
    if with_analysis:
        claims.write_analysis(d, ANALYSIS)
    plan_ = {"goal": "g", "repo": str(tmp_path), "base": "HEAD", "analysis": ANALYSIS, "steps": [step("s1", files=["a.py"])]}
    state = {"status": "running", "integration": {"ok": integration_ok}, "security": OK, "verify": OK,
             "steps": {"s1": {"status": "done", "claimed": claimed, "commit": sha}}}
    monkeypatch.setattr(plan, "accept", lambda s, w: {"cmd": s["acceptance"], "ok": acc_ok, "rc": 0 if acc_ok else 1})
    return plan.Ctx("p1", d, plan_, state)


def _fake_llm(monkeypatch, text='{"findings": []}', ok=True):
    calls = []

    def fake(chain, prompt, opts, trace):
        calls.append(prompt)
        return SimpleNamespace(ok=ok, text=text, error="" if ok else "boom", backend="fake"), 0.001, {"input": 10, "output": 5}
    monkeypatch.setattr(plan.agents, "call", fake)
    return calls


def test_flag_off_returns_off_and_calls_no_model(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOW_CLAIMS", "0")
    calls = _fake_llm(monkeypatch)
    assert plan.run_claims(_ctx(tmp_path, monkeypatch, claimed="FAIL")) == {"status": "off"}
    assert calls == []


def test_plan_without_analysis_is_skipped_and_calls_no_model(tmp_path, monkeypatch):
    calls = _fake_llm(monkeypatch)
    out = plan.run_claims(_ctx(tmp_path, monkeypatch, with_analysis=False))
    assert out["status"] == "skipped" and calls == []


def test_claimed_pass_with_failing_acceptance_blocks_and_ship_says_no_go(tmp_path, monkeypatch):
    _fake_llm(monkeypatch)
    ctx = _ctx(tmp_path, monkeypatch, claimed="PASS", acc_ok=False)
    out = plan.run_claims(ctx)
    assert out["status"] == "ok" and any("bước s1 khai PASS" in b for b in out["blockers"])
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.py"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "NO-GO" and any(b.startswith("claims: bước s1 khai PASS") for b in verdict["blockers"])


def test_honest_pass_is_not_blocked_and_ship_stays_go(tmp_path, monkeypatch):
    _fake_llm(monkeypatch)
    ctx = _ctx(tmp_path, monkeypatch, claimed="PASS", acc_ok=True)
    out = plan.run_claims(ctx)
    assert out["blockers"] == [] and out["machine"]["rows"][0]["match"] is True
    ctx.state["claims"] = out
    assert method.decide(ctx.plan, ctx.state, {}, ["a.py"], True, None, claims=plan._claims_for_decide(ctx.state))["decision"] == "GO"


def test_llm_mismatch_reaches_ship_as_a_blocker(tmp_path, monkeypatch):
    reply = json.dumps({"findings": [{"kind": "machine", "item": "tests pass", "verdict": "mismatch", "evidence": "README.md:1 says 1 failed"}]})
    calls = _fake_llm(monkeypatch, text=reply)
    ctx = _ctx(tmp_path, monkeypatch)
    out = plan.run_claims(ctx)
    assert len(calls) == 1 and out["llm"]["findings"][0]["verdict"] == "mismatch"
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.py"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "NO-GO" and any("tests pass" in b and "1 failed" in b for b in verdict["blockers"])


def test_llm_mismatch_without_checkable_evidence_stays_go(tmp_path, monkeypatch):
    reply = json.dumps({"findings": [{"kind": "machine", "item": "tests pass", "verdict": "mismatch", "evidence": "log says 1 failed"}]})
    _fake_llm(monkeypatch, text=reply)
    ctx = _ctx(tmp_path, monkeypatch)
    out = plan.run_claims(ctx)
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.py"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "GO" and verdict["blockers"] == []
    assert len(verdict["recommended"]) == 1 and verdict["recommended"][0].startswith("claims: [chưa kiểm được] tests pass")


def test_integration_failed_is_partial_and_calls_no_model(tmp_path, monkeypatch):
    calls = _fake_llm(monkeypatch)
    out = plan.run_claims(_ctx(tmp_path, monkeypatch, integration_ok=False))
    assert out["status"] == "partial" and "llm" not in out and calls == []


def test_crashed_check_is_not_read_as_clean_by_ship():
    assert plan._claims_for_decide({"claims": {"status": "off"}}) is None
    assert plan._claims_for_decide({}) is None
    crashed = plan._claims_for_decide({"claims": {"status": "not_run", "decision": "error", "error": "KeyError: x"}})
    assert crashed["blockers"] == [] and crashed["llm"]["status"] == "not_run"


def test_create_writes_analysis_md_next_to_plan_json(env):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    text = (env.home / "plans" / pid / "analysis.md").read_text(encoding="utf-8")
    assert "## Root cause" in text and "## Spec" in text and "`README.md`" in text


def test_approved_plan_runs_the_claims_check_and_the_report_shows_it(env):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    d = env.home / "plans" / pid
    state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    assert state["claims"]["status"] == "ok" and [r["step"] for r in state["claims"]["machine"]["rows"]] == ["s1"]
    rep = (d / "report.md").read_text(encoding="utf-8")
    assert "## Kiểm tra đối chiếu" in rep and "bước s1: khai không khai, chạy lại PASS" in rep


def test_claim_reads_the_last_claim_line_only():
    assert plan._claim("done\nCLAIM: PASS") == "PASS"
    assert plan._claim("CLAIM: FAIL\nCLAIM: PASS") == "PASS"
    assert plan._claim("no claim here") is None
    assert plan._claim("CLAIM: maybe") is None


def test_tampered_analysis_md_is_a_blocker(tmp_path, monkeypatch):
    _fake_llm(monkeypatch)
    monkeypatch.delenv("FLOW_CLAIMS", raising=False)
    ctx = _ctx(tmp_path, monkeypatch)
    (ctx.d / "analysis.md").write_text("changed", encoding="utf-8")
    out = plan.run_claims(ctx)
    assert any("analysis.md" in b for b in out["blockers"])


def test_create_stores_analysis_sha(env):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    d = env.home / "plans" / pid
    state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    sha = state["analysis_sha"]
    assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
    assert sha == claims.file_sha(d / "analysis.md")
