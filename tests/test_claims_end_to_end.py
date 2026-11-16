"""End to end claims check: the real acceptance rerun at each step's commit, symlinks and Windows paths in research, redaction of the prompt, and the SHIP decision."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

import test_plan
from flow_engine import claims, method, plan

step = test_plan.step
ANALYSIS = {"research": [{"file": "README.md", "finding": "the repo only holds a readme"}], "root_cause": "nothing implements the goal yet",
            "approach": "one module per step", "alternatives": [{"option": "one big file", "rejected_because": "no parallel steps"}],
            "fix": "small modules, each checked by its own acceptance command", "blast_radius": {"affected": ["README.md"], "risks": "none"},
            "spec": "each step writes its file"}
OK = {"status": "ok", "hard": [], "soft": [], "n_hard": 0, "n_soft": 0}
GOOD_ONLY = "python3 -c \"import sys; sys.exit(0 if open('a.txt').read().strip() == 'good' else 1)\""


def _git(repo, *a):
    return subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, text=True).stdout.strip()


def _ctx(tmp_path, monkeypatch, sha_of_a, claimed="PASS"):
    """A real repo at tmp_path with a.txt committed at sha_of_a (the step's commit); the plan worktree holds README.md only."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "flow@localhost")
    _git(tmp_path, "config", "user.name", "flow-engine")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "init")
    d = tmp_path / "plan"
    (d / "wt" / "_plan").mkdir(parents=True)
    (d / "wt" / "_plan" / "README.md").write_text("hello\n")
    claims.write_analysis(d, ANALYSIS)
    (tmp_path / "a.txt").write_text(sha_of_a + "\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "step s1")
    sha = _git(tmp_path, "rev-parse", "--short", "HEAD")
    plan_ = {"goal": "g", "repo": str(tmp_path), "base": "HEAD", "analysis": ANALYSIS, "steps": [step("s1", files=["a.txt"], acc=GOOD_ONLY)]}
    state = {"status": "running", "integration": {"ok": True}, "security": OK, "verify": OK,
             "steps": {"s1": {"status": "done", "claimed": claimed, "commit": sha}}}
    return plan.Ctx("p1", d, plan_, state)


def _fake_llm(monkeypatch, text='{"findings": []}', ok=True):
    prompts = []

    def fake(chain, prompt, opts, trace):
        prompts.append(prompt)
        return SimpleNamespace(ok=ok, text=text, error="" if ok else "boom", backend="fake"), 0.001, {"input": 1, "output": 1}
    monkeypatch.setattr(plan.agents, "call", fake)
    return prompts


def test_rerun_uses_the_step_commit_not_the_later_tree(tmp_path, monkeypatch):
    _fake_llm(monkeypatch)
    monkeypatch.delenv("FLOW_CLAIMS", raising=False)
    # the good commit G is the step's own; B is a later commit that breaks a.txt
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "flow@localhost")
    _git(tmp_path, "config", "user.name", "flow-engine")
    (tmp_path / "README.md").write_text("hello\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / "a.txt").write_text("good\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "G")
    g = _git(tmp_path, "rev-parse", "--short", "HEAD")
    (tmp_path / "a.txt").write_text("bad\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "B")
    b = _git(tmp_path, "rev-parse", "--short", "HEAD")
    d = tmp_path / "plan"
    (d / "wt" / "_plan").mkdir(parents=True)
    (d / "wt" / "_plan" / "README.md").write_text("hello\n")
    (d / "wt" / "_plan" / "a.txt").write_text("bad\n")  # the plan worktree content must not decide the verdict
    claims.write_analysis(d, ANALYSIS)
    plan_ = {"goal": "g", "repo": str(tmp_path), "base": "HEAD", "analysis": ANALYSIS, "steps": [step("s1", files=["a.txt"], acc=GOOD_ONLY)]}

    def ctx_at(sha):
        state = {"status": "running", "integration": {"ok": True}, "security": OK, "verify": OK,
                 "steps": {"s1": {"status": "done", "claimed": "PASS", "commit": sha}}}
        return plan.Ctx("p1", d, plan_, state)

    out_b = plan.run_claims(ctx_at(b))
    assert any(x.startswith("claims: bước s1 khai PASS") for x in out_b["blockers"])
    out_g = plan.run_claims(ctx_at(g))
    assert not any(x.startswith("claims: bước s1 khai PASS") for x in out_g["blockers"])
    assert out_g["machine"]["rows"] == [{"step": "s1", "claimed": "PASS", "actual": True, "match": True}]


def test_pass_claim_at_failing_commit_is_blocked_and_ship_says_no_go(tmp_path, monkeypatch):
    _fake_llm(monkeypatch)
    ctx = _ctx(tmp_path, monkeypatch, sha_of_a="bad")
    out = plan.run_claims(ctx)
    assert any(x.startswith("claims: bước s1 khai PASS") for x in out["blockers"])
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.txt"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "NO-GO"


def test_symlink_inside_worktree_to_outside_file_is_a_research_problem(tmp_path):
    d = tmp_path / "plan"
    wt = d / "wt" / "_plan"
    wt.mkdir(parents=True)
    (wt / "README.md").write_text("hello\n")
    (tmp_path / "outside.md").write_text("secret outside\n")
    try:
        (wt / "link.md").symlink_to(tmp_path / "outside.md")
        (wt / "ok.md").symlink_to(wt / "README.md")
    except OSError:
        pytest.skip("cannot create symlinks here")
    problems = claims.check_research({"research": [{"file": "link.md", "finding": "x"}]}, str(wt))
    assert len(problems) == 1 and problems[0].startswith("research không căn cứ")
    assert claims.check_research({"research": [{"file": "ok.md", "finding": "x"}]}, str(wt)) == []


@pytest.mark.parametrize("path", [r"C:\Windows\x.md", r"..\outside.md", "/etc/passwd", "sub\\file.md", "../outside.md"])
def test_windows_and_escaping_paths_are_refused(tmp_path, path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "README.md").write_text("hello\n")
    assert claims.inside_root(wt, path) is None
    assert claims.check_research({"research": [{"file": path, "finding": "x"}]}, str(wt))


def test_plain_readme_path_passes(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "README.md").write_text("hello\n")
    assert claims.inside_root(wt, "README.md") is not None
    assert claims.check_research({"research": [{"file": "README.md", "finding": "x"}]}, str(wt)) == []


def test_secret_in_claims_text_never_reaches_the_llm_prompt(tmp_path, monkeypatch):
    prompts = _fake_llm(monkeypatch)
    monkeypatch.delenv("FLOW_CLAIMS", raising=False)
    ctx = _ctx(tmp_path, monkeypatch, sha_of_a="good")
    fake_key = "sk-" + "TESTFAKE" + "abcdef123456"
    ctx.state["steps"]["s1"]["summary"] = f"gọi API bằng {fake_key} rồi xong"
    plan.run_claims(ctx)
    assert len(prompts) == 1
    assert fake_key not in prompts[0] and "TESTFAKE" not in prompts[0]
    assert "gọi API bằng" in prompts[0]


def test_llm_mismatch_with_checkable_evidence_is_no_go(tmp_path, monkeypatch):
    import json
    reply = json.dumps({"findings": [{"kind": "machine", "item": "tests pass", "verdict": "mismatch", "evidence": "README.md:1 says 1 failed"}]})
    _fake_llm(monkeypatch, text=reply)
    ctx = _ctx(tmp_path, monkeypatch, sha_of_a="good")
    out = plan.run_claims(ctx)
    assert out["llm"]["findings"][0]["verifiable"] is True
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.txt"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "NO-GO"
    assert any(b.startswith("claims: [đối chiếu] tests pass") for b in verdict["blockers"])


def test_llm_mismatch_without_checkable_evidence_is_recommended_and_go(tmp_path, monkeypatch):
    import json
    reply = json.dumps({"findings": [{"kind": "machine", "item": "tests pass", "verdict": "mismatch", "evidence": "log says 1 failed"}]})
    _fake_llm(monkeypatch, text=reply)
    ctx = _ctx(tmp_path, monkeypatch, sha_of_a="good")
    out = plan.run_claims(ctx)
    assert out["llm"]["findings"][0]["verifiable"] is False
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.txt"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "GO"
    assert len(verdict["recommended"]) == 1 and verdict["recommended"][0].startswith("claims: [chưa kiểm được]")


def test_failed_llm_call_is_not_run_and_ship_says_no_go(tmp_path, monkeypatch):
    _fake_llm(monkeypatch, ok=False)
    ctx = _ctx(tmp_path, monkeypatch, sha_of_a="good")
    out = plan.run_claims(ctx)
    assert out["llm"]["status"] == "not_run"
    ctx.state["claims"] = out
    verdict = method.decide(ctx.plan, ctx.state, {}, ["a.txt"], True, None, claims=plan._claims_for_decide(ctx.state))
    assert verdict["decision"] == "NO-GO"
    assert any(b.startswith("claims: lượt đối chiếu LLM không chạy") for b in verdict["blockers"])
