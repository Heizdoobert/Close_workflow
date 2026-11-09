"""Step 1, the interview: rounds of one question with a guess, a confirmed brief, and the plan drafted from it. Uses the fake agents from test_plan."""

import json
import sys

import pytest

from flow_engine import cli, interview, plan
from test_plan import env, step  # noqa: F401  (env is the shared fake-agent fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
KEY = "sk-ant-FAKE1234567890ABCDEF"
ASK = {"coverage": {**{k: "known" for k in ("who", "why", "success")}}, "hypothesis": "You want a report of failed logins", "confidence": 40, "missing": "who reads it", "question": "Who reads the report?", "guess": "the on-call engineer", "brief": None}
BRIEF = {"intent": "give the on-call engineer a daily list of failed logins", "who": "the on-call engineer", "why": "attacks go unnoticed because nobody reads auth.log", "scope": "one script, no UI",
         "impact": ["auth.log stays untouched", "the login path must not change"], "non_goals": ["no alerting"], "constraints": ["stdlib only"], "success": "the script prints yesterday's failed logins",
         "risks": ["empty log file", "log rotated at midnight"], "assumptions": ["logs are in /var/log"]}
KNOWN = {k: "known" for k in interview.DIMS}
DONE = {"hypothesis": "daily failed-login list for on-call", "confidence": 96, "coverage": KNOWN, "missing": "", "question": "", "guess": "", "brief": BRIEF}


def script(env, monkeypatch, *replies):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    monkeypatch.setenv("FAKE_INTERVIEW", json.dumps(list(replies)))


def answers(*texts):
    it = iter(texts)
    return lambda prompt="": next(it)


def test_a_vague_reply_without_a_question_is_rejected_then_repaired(env, monkeypatch):
    script(env, monkeypatch, {**ASK, "question": ""}, ASK)
    r = interview.step("g", [], plan.agents.chain_from_env("claude:haiku", None), str(env.repo))
    assert r["question"] == "Who reads the report?" and r["brief"] is None
    script(env, monkeypatch, {**ASK, "question": ""})
    with pytest.raises(interview.InterviewError):
        interview.step("g", [], plan.agents.chain_from_env("claude:haiku", None), str(env.repo))


def test_a_confident_round_returns_the_brief_and_no_question(env, monkeypatch):
    script(env, monkeypatch, DONE)
    r = interview.step("g", [], plan.agents.chain_from_env("claude:haiku", None), str(env.repo))
    assert r["brief"]["intent"].startswith("give the on-call") and r["question"] == ""


def test_out_of_rounds_forces_a_brief_with_the_assumptions(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    script(env, monkeypatch, {**DONE, "confidence": 70})
    r = interview.step("g", [{"q": "a?", "a": "b"}] * 3, plan.agents.chain_from_env("claude:haiku", None), str(env.repo), rounds=3)
    assert "Do not ask another" in seen[0] and r["brief"]["confidence"] == 70


def test_interview_step_prints_one_round_as_json_for_agents(env, monkeypatch, capsys, tmp_path):
    script(env, monkeypatch, ASK)
    qa = tmp_path / "qa.json"
    qa.write_text(json.dumps([{"q": "earlier?", "a": "yes"}]))
    assert cli.main(["interview-step", "report", "failed", "logins", "--qa", str(qa)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["question"] == "Who reads the report?" and out["confidence"] == 40


def test_the_brief_must_be_confirmed_before_a_plan_is_drafted(env, monkeypatch, tmp_path, capsys):
    script(env, monkeypatch, DONE)
    f = tmp_path / "brief.json"
    f.write_text(json.dumps({**BRIEF, "confidence": 96, "qa": [], "coverage": KNOWN}))
    assert cli.main(["plan", "report", "--brief", str(f)]) == 1
    assert "not confirmed" in capsys.readouterr().err
    assert cli.main(["plan", "report", "--brief", str(f), "--confirmed"]) == 0
    pid = plan.plans_dir().glob("*").__next__().name
    assert json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8"))["brief"]["confirmed"] is True


def test_the_interactive_loop_asks_listens_and_drafts_the_plan_from_the_confirmed_brief(env, monkeypatch):
    prompts = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (prompts.append(prompt), real(chain, prompt, o, t))[1])
    script(env, monkeypatch, ASK, DONE)
    out = []
    pid = interview.run_cli("failed logins report", ask=answers(f"the on-call engineer, token {KEY}", "y"), out=out.append)
    stored = json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8"))
    assert stored["brief"]["confirmed"] and stored["brief"]["qa"][0]["q"] == "Who reads the report?"
    assert KEY not in json.dumps(stored) and "[REDACTED]" in json.dumps(stored)
    planner = next(p for p in prompts if "planning agent" in p)
    assert "Requirements the user confirmed" in planner and "no alerting" in planner
    assert "[40%]" in out[0] and "GUESS: the on-call engineer" in out[0] and "[96%]" in out[1]
    md = plan.show(pid)
    assert "Brief (confirmed by the user)" in md


def test_a_correction_to_the_brief_earns_another_round(env, monkeypatch):
    script(env, monkeypatch, DONE, {**DONE, "brief": {**BRIEF, "non_goals": ["no alerting", "no email"]}})
    pid = interview.run_cli("g", ask=answers("no email either", "y"), out=lambda *_: None)
    b = json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8"))["brief"]
    assert "no email" in b["non_goals"] and b["qa"][0]["a"] == "no email either"


def test_enter_accepts_the_guess_and_stop_ends_with_assumptions(env, monkeypatch):
    script(env, monkeypatch, ASK, ASK, {**DONE, "confidence": 80})
    pid = interview.run_cli("g", ask=answers("", "/stop", "", "y"), out=lambda *_: None)  # the empty answer before "y" is not a confirmation
    b = json.loads((env.home / "plans" / pid / "plan.json").read_text(encoding="utf-8"))["brief"]
    assert b["qa"][0]["a"].startswith("(chấp nhận đoán)") and b["confidence"] == 80
    assert any("only accepted my guess: Who reads the report?" in a for a in b["assumptions"])  # code adds it, the agent did not have to


def test_report_and_step_agents_carry_the_brief(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    script(env, monkeypatch, DONE)
    pid = interview.run_cli("g", ask=answers("y"), out=lambda *_: None)
    assert plan.approve(pid, wait_for=True) == "done"
    assert "non-goals: no alerting" in next(p for p in seen if "Your step [" in p)
    rep = (env.home / "plans" / pid / "report.md").read_text(encoding="utf-8")
    assert "Làm rõ yêu cầu" in rep and "no alerting" in rep
    row = next(l for l in rep.splitlines() if l.startswith("| 1 "))
    assert "OK" in row and "0 câu hỏi" in row and "agent tự báo" in row


def test_interview_needs_a_terminal(env, monkeypatch, capsys):
    monkeypatch.setattr(interview, "tty", lambda: False)
    assert cli.main(["interview", "something", "vague"]) == 1
    assert "interview-step" in capsys.readouterr().err


# --- does the interview reach the root cause, the impact and the risks? ------------------------------------------------

def test_the_prompt_makes_the_agent_cover_why_impact_and_risks(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    script(env, monkeypatch, ASK)
    interview.step("g", [], plan.agents.chain_from_env("claude:haiku", None), str(env.repo))
    for needle in ("root cause", "must keep working exactly as now", "what could go wrong", "Never give 95 or more while any of the seven is unknown", '"coverage"'):
        assert needle in seen[0], needle


def test_confidence_cannot_reach_95_while_a_dimension_is_unknown(env, monkeypatch):
    gap = {**DONE, "coverage": {**KNOWN, "impact": "unknown", "risks": "unknown"}}
    script(env, monkeypatch, gap, ASK)  # the first reply is rejected, the repair round asks a question
    r = interview.step("g", [], plan.agents.chain_from_env("claude:haiku", None), str(env.repo))
    assert r["brief"] is None and r["question"]
    with pytest.raises(interview.InterviewError, match="impact, risks"):
        interview._parse(json.dumps(gap), False)
    script(env, monkeypatch, gap)
    with pytest.raises(interview.InterviewError, match="usable reply"):
        interview.step("g", [], plan.agents.chain_from_env("claude:haiku", None), str(env.repo))


def test_out_of_rounds_with_a_gap_keeps_the_gap_in_the_brief(env, monkeypatch):
    script(env, monkeypatch, {**DONE, "confidence": 80, "coverage": {**KNOWN, "impact": "unknown"}, "brief": {k: v for k, v in BRIEF.items() if k not in ("why", "impact")}})
    r = interview.step("g", [{"q": "a?", "a": "b"}] * 3, plan.agents.chain_from_env("claude:haiku", None), str(env.repo), rounds=3)
    b = interview.clean({**r["brief"], "qa": []})
    assert b["impact"] == [interview.NONE] and b["why"] == interview.NONE and "impact is unknown, not confirmed" in b["assumptions"]


def test_a_brief_without_why_or_impact_is_not_accepted():
    bad = interview.brief_problems({**{k: v for k, v in BRIEF.items() if k not in ("why", "impact")}, "confirmed": True})
    assert any("brief.why" in p for p in bad) and any("brief.impact" in p for p in bad)


def test_why_impact_and_risks_reach_the_planner_the_step_agents_and_the_report(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    script(env, monkeypatch, DONE)
    pid = interview.run_cli("g", ask=answers("y"), out=lambda *_: None)
    assert "why (root problem): attacks go unnoticed" in next(p for p in seen if "planning agent" in p)
    assert plan.approve(pid, wait_for=True) == "done"
    assert "must not change: auth.log stays untouched" in next(p for p in seen if "Your step [" in p)
    rep = (env.home / "plans" / pid / "report.md").read_text(encoding="utf-8")
    assert "Vì sao (vấn đề gốc)" in rep and "log rotated at midnight" in rep and "Bao phủ 7 mục" in rep
    assert "7/7 mục" in next(l for l in rep.splitlines() if l.startswith("| 1 "))


def test_an_unusable_agent_reply_is_an_error_message_not_a_traceback(env, monkeypatch, capsys):
    script(env, monkeypatch, {"nonsense": True})
    assert cli.main(["interview-step", "something", "vague"]) == 1
    assert "usable reply" in capsys.readouterr().err


def test_a_coverage_value_with_an_explanation_still_reads_as_its_leading_word():
    c = interview.coverage({"who": "Assumed: the nightly job", "why": "known", "impact": "unknown: nothing checked", "success": "maybe", "constraints": None})
    assert c["who"] == "assumed" and c["why"] == "known" and c["impact"] == "unknown" and c["success"] == "unknown" and c["constraints"] == "unknown" and c["risks"] == "unknown"
