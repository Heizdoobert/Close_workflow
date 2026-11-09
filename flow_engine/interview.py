"""Step 1 of the method, the agent-skills `interview-me` skill as code: find out what the user actually wants before anything is planned.

One agent call per round (read-only: it may read the repo, so it never asks what the code already shows) returns a hypothesis, an honest confidence number and
ONE question with a guess attached. Rounds repeat until confidence >= 95 or FLOW_INTERVIEW_ROUNDS (6); then it writes a brief. The user confirms the brief, and
only then is the plan drafted (`plan.create(..., brief=...)`), so the plan starts from cleared requirements. What the harness checks: the brief is complete and was
confirmed (`--confirmed`, `y` in the CLI loop). What it cannot check: that a person really answered. The report says how many questions were asked.
"""

import json
import os
import sys

from flow_engine import agents, bridge
from flow_engine.secrets import redact

CONFIDENT = 95
PROMPT = """You are the interview agent of an automated engineering system (the interview-me method). The user gave a task; before anything is planned you find out what they actually want, one question at a time. You may read the repository (read-only): never ask the user what the code already shows.
Task: {goal}
Questions already answered:
{qa}
{wrap}Reply with ONLY one JSON object, no prose:
{{"hypothesis":"one sentence: what the user actually wants","confidence":0-100,"missing":"what is still unclear (empty when confident)","question":"ONE focused question; empty only when confidence >= {conf}","guess":"your answer to that question and why","brief":null}}
Rules: be honest with the number: below {conf} while who it is for, why, what success looks like, the binding constraint or the non-goals are unknown. Attach a concrete guess to the question (a user reacts faster to a wrong guess). Listen for "should want" answers (buzzwords, convention) and then ask what they would want if they did not have to justify it. Never answer for the user.
When confidence >= {conf}: set question to "" and brief to {{"intent":"what and why","scope":"what is included","non_goals":["what is not"],"constraints":["binding limits"],"success":"how the user will know it is done","assumptions":["things you assumed and the user did not confirm"]}}."""
WRAP = "You have asked enough questions. Do not ask another: set confidence honestly, list every unconfirmed point in brief.assumptions and write the brief now.\n"


class InterviewError(Exception):
    pass


def _s(x, n=600):
    return str(x or "").strip()[:n]


def _list(x, n=6):
    return [_s(i, 200) for i in (x if isinstance(x, list) else []) if _s(i)][:n]


def brief_problems(b):
    """What is wrong with a brief (empty list = fine)."""
    if not isinstance(b, dict):
        return ["brief is not an object"]
    out = [f"brief.{k} missing or too short" for k, n in (("intent", 20), ("success", 10)) if len(_s(b.get(k))) < n]
    if not isinstance(b.get("qa", []), list):
        out.append("brief.qa must be a list")
    if not b.get("confirmed"):
        out.append("the user has not confirmed the brief (pass --confirmed only after they said yes)")
    return out


def clean(b, cost=0.0):
    """The known keys only, cut and redacted, so plan.json stays small and holds no key a user pasted into an answer."""
    qa = [{"q": redact(_s(x.get("q"), 300)), "a": redact(_s(x.get("a"), 500))} for x in b.get("qa", []) if isinstance(x, dict)][:12]
    out = {"intent": _s(b.get("intent")), "scope": _s(b.get("scope")), "non_goals": _list(b.get("non_goals")), "constraints": _list(b.get("constraints")), "success": _s(b.get("success")),
           "assumptions": _list(b.get("assumptions")), "confidence": max(0, min(100, int(b.get("confidence") or 0))), "qa": qa, "confirmed": True, "cost_usd": round(float(b.get("cost_usd") or cost), 4)}
    return {k: (redact(v) if isinstance(v, str) else v) for k, v in out.items()}


def text(b, limit=900):
    """The brief as the planner and the step agents read it: requirements the user confirmed."""
    t = (f"Requirements the user confirmed (follow them):\nintent: {b['intent']}\nscope: {b['scope']}\nnon-goals: {'; '.join(b['non_goals']) or '-'}\n"
         f"constraints: {'; '.join(b['constraints']) or '-'}\nsuccess: {b['success']}\nnot confirmed, assumed: {'; '.join(b['assumptions']) or '-'}\n\n")
    return t.encode()[:limit].decode(errors="ignore")


def md(b):
    if not b:
        return []
    return ["Brief (confirmed by the user):", f"- intent: {b['intent'][:140]}", f"- success: {b['success'][:140]}", f"- non-goals: {'; '.join(b['non_goals'])[:140] or '-'}", ""]


def report_md(b):
    if not b:
        return ["_Không phỏng vấn: yêu cầu đi thẳng vào plan, không có brief đã xác nhận._"]
    return ([f"**Ý định:** {b['intent']}", f"**Phạm vi:** {b['scope']}", f"**Ngoài phạm vi:** {'; '.join(b['non_goals']) or '-'}", f"**Ràng buộc:** {'; '.join(b['constraints']) or '-'}", f"**Thành công là:** {b['success']}",
             f"**Chưa xác nhận, đã giả định:** {'; '.join(b['assumptions']) or '-'}", f"**Độ tự tin khi chốt:** {b['confidence']}% sau {len(b['qa'])} câu hỏi"]
            + [f"- hỏi: {x['q']} → đáp: {x['a']}" for x in b["qa"]])


def step(goal, qa, chain, wd, rounds=None, trace=None):
    """One round. Returns {"confidence", "hypothesis", "missing", "question", "guess", "brief"} (brief only when confident or out of rounds)."""
    rounds = rounds or int(os.environ.get("FLOW_INTERVIEW_ROUNDS", "6"))
    wrap = len(qa) >= rounds
    qa_txt = "\n".join(f"Q: {x['q']}\nA: {x['a']}" for x in qa) or "(none yet)"
    prompt = PROMPT.format(goal=goal, qa=qa_txt, wrap=WRAP if wrap else "", conf=CONFIDENT)
    cost = 0.0
    for _ in range(2):  # one repair round, like the planner
        res, c, _t = agents.call(chain, bridge.recall(goal) + prompt, agents.Opts(wd, agents.READ_TOOLS, float(os.environ.get("FLOW_INTERVIEW_USD", "0.10")), 300), trace if trace is not None else [])
        cost += c
        if not res.ok:
            raise InterviewError(f"interview agent failed: {res.error}")
        try:
            r = _parse(res.text, wrap)
            r["cost_usd"] = round(cost, 4)
            return r
        except InterviewError as e:
            prompt += f"\n\nYour previous reply was rejected ({e}). Reply with ONLY the corrected JSON object."
    raise InterviewError("interview agent did not return a usable reply")


def _parse(reply, wrap):
    from flow_engine.plan import extract_json, PlanError
    try:
        o = extract_json(reply)
    except PlanError as e:
        raise InterviewError(str(e))
    try:
        conf = max(0, min(100, int(o.get("confidence"))))
    except (TypeError, ValueError):
        raise InterviewError("confidence must be a number 0-100")
    r = {"confidence": conf, "hypothesis": _s(o.get("hypothesis"), 300), "missing": _s(o.get("missing"), 300), "question": _s(o.get("question"), 400), "guess": _s(o.get("guess"), 400), "brief": None}
    if conf >= CONFIDENT or wrap:
        b = o.get("brief")
        if not isinstance(b, dict) or len(_s(b.get("intent"))) < 20 or len(_s(b.get("success"))) < 10:
            raise InterviewError("confident enough, so brief needs intent and success")
        r["brief"] = {**b, "confidence": conf}
        r["question"] = ""
    elif not r["question"]:
        raise InterviewError(f"confidence {conf} is below {CONFIDENT}, so a question is required")
    return r


def run_cli(goal, model=None, ask=input, out=print):
    """The interactive loop: ask, listen, and once the user confirms the brief, draft the plan. Returns the plan id."""
    from flow_engine import plan
    wd = plan.repo()
    chain, qa, cost, rounds = agents.chain_from_env("claude:haiku", model), [], 0.0, int(os.environ.get("FLOW_INTERVIEW_ROUNDS", "6"))
    while True:
        r = step(goal, qa, chain, wd, rounds)
        cost += r["cost_usd"]
        if r["brief"]:
            b = {**r["brief"], "qa": qa, "cost_usd": cost}
            out(f"[{r['confidence']}%] Brief:\n" + "\n".join(report_md(clean({**b, 'confirmed': True}))))
            ans = ask("Đúng chưa? [Y = đúng, bắt đầu lập plan / gõ chỗ cần sửa] ").strip()
            if ans.lower() in ("", "y", "yes", "đúng", "dung"):
                return plan.create(goal, model, brief={**b, "confirmed": True})
            qa.append({"q": "Chỗ nào của brief chưa đúng?", "a": ans})
            rounds += 1  # a correction earns one more round
            continue
        out(f"[{r['confidence']}%] {r['hypothesis']}\nQ: {r['question']}\nGUESS: {r['guess']}")
        ans = ask("> (Enter = chấp nhận đoán, /stop = chốt với giả định hiện có) ").strip()
        if ans == "/stop":
            rounds = len(qa)  # next round must write the brief
            continue
        qa.append({"q": r["question"], "a": ans or f"(chấp nhận đoán) {r['guess']}"})


def main_step(goal, qa_file=None, model=None):
    """For agents without a terminal: one round as JSON on stdout. The agent asks the user, appends {q, a} to the qa file and calls again."""
    from flow_engine import plan
    qa = json.loads(open(qa_file, encoding="utf-8").read()) if qa_file and os.path.exists(qa_file) else []
    r = step(goal, qa, agents.chain_from_env("claude:haiku", model), plan.repo())
    if r["brief"]:
        r["brief"] = {**r["brief"], "qa": qa, "cost_usd": r["cost_usd"]}
    print(json.dumps(r, ensure_ascii=False))
    return 0


def tty():
    return sys.stdin.isatty()
