"""method.decide with the optional claims argument: mismatch blocks, unsupported and a LLM pass that did not run are recommendations."""

from flow_engine import method, plan

ST = {"steps": {"s1": {"status": "done"}}, "integration": {"ok": True},
      "security": {"status": "ok", "hard": [], "soft": [], "n_hard": 0, "n_soft": 0},
      "verify": {"status": "ok", "hard": [], "soft": [], "n_hard": 0, "n_soft": 0}}


def _decide(claims=None):
    return method.decide({}, ST, {}, ["a.py"], True, None, claims=claims)


def test_claims_none_gives_go_as_before():
    out = _decide()
    assert out["decision"] == "GO" and out["blockers"] == [] and out["recommended"] == []
    assert set(out) == {"decision", "blockers", "recommended", "risks"}


def test_claims_blockers_make_no_go_and_the_text_is_kept():
    text = "claims: bước s1 khai PASS nhưng acceptance fail"
    out = _decide({"blockers": [text]})
    assert out["decision"] == "NO-GO" and text in out["blockers"]


def test_llm_mismatch_finding_is_a_blocker():
    out = _decide({"llm": {"status": "ok", "findings": [{"verdict": "mismatch", "item": "tests pass", "evidence": "report says 5 passed, log says 1 failed", "verifiable": True}]}})
    assert out["decision"] == "NO-GO"
    assert any(b.startswith("claims: [đối chiếu] tests pass") and "1 failed" in b for b in out["blockers"])


def test_llm_mismatch_without_evidence_is_a_recommendation():
    out = _decide({"llm": {"status": "ok", "findings": [{"verdict": "mismatch", "item": "tests pass", "evidence": "log says 1 failed"}]}})
    assert out["decision"] == "GO" and out["blockers"] == []
    assert len(out["recommended"]) == 1 and out["recommended"][0].startswith("claims: [chưa kiểm được] tests pass")


def test_llm_unsupported_finding_is_a_recommendation_only():
    out = _decide({"llm": {"status": "ok", "findings": [{"verdict": "unsupported", "item": "root cause is X", "evidence": "no file shows X"}]}})
    assert out["decision"] == "GO" and out["blockers"] == []
    assert len(out["recommended"]) == 1 and out["recommended"][0].startswith("MỘT PHẦN")


def test_llm_not_run_is_a_blocker():
    out = _decide({"llm": {"status": "not_run", "error": "timeout"}})
    assert out["decision"] == "NO-GO"
    assert any("lượt đối chiếu LLM không chạy (timeout)" in b for b in out["blockers"])


def test_partial_claims_dict_does_not_raise():
    assert _decide({})["decision"] == "GO"
    assert _decide({"llm": "not a dict", "blockers": [1, None]})["decision"] == "GO"


def test_claims_check_crash_reaches_decide_as_no_go():
    c = plan._claims_for_decide({"claims": {"status": "not_run", "decision": "error", "error": "KeyError: x"}})
    out = _decide(c)
    assert out["decision"] == "NO-GO" and any("không chạy" in b for b in out["blockers"])


def test_llm_not_run_for_budget_is_a_blocker():
    c = plan._claims_for_decide({"claims": {"status": "ok", "blockers": [], "llm": {"status": "not_run", "error": "plan budget used up"}}})
    out = _decide(c)
    assert out["decision"] == "NO-GO" and any("plan budget used up" in b for b in out["blockers"])
