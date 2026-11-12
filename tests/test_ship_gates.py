"""SHIP decision holes found by the war-room review: a persona answer of the wrong shape must not become a GO."""
import pytest

from flow_engine import method

ST = {"steps": {"s1": {"status": "done"}}, "integration": {"ok": True},
      "security": {"status": "ok", "hard": [], "soft": [], "n_hard": 0, "n_soft": 0},
      "verify": {"status": "ok", "hard": [], "soft": [], "n_hard": 0, "n_soft": 0}}


def decide(personas, files=("a.py",), review=None):
    return method.decide({}, ST, personas, list(files), False, review)


def finding(severity="low", line=1, file="a.py"):
    return {"file": file, "line": line, "severity": severity, "problem": "p", "fix": "f"}


@pytest.mark.parametrize("answer", [{}, {"findings": "none"}, {"findings": ["a.py: bad"]}, ["a.py"], None])
def test_a_persona_answer_that_is_not_the_findings_schema_is_not_run_and_blocks(answer):
    r = method.parse_persona("code-reviewer", answer, {"a.py"})
    assert r["status"] == "not_run"
    out = decide({"code-reviewer": r})
    assert out["decision"] == "NO-GO" and any("không chạy" in b for b in out["blockers"])


def test_an_empty_findings_list_is_still_a_clean_answer():
    r = method.parse_persona("code-reviewer", {"findings": []}, {"a.py"})
    assert r["status"] == "ok" and decide({"code-reviewer": r})["decision"] == "GO"


def test_an_unknown_severity_counts_as_critical():
    r = method.parse_persona("code-reviewer", {"findings": [finding("urgent")]}, {"a.py"})
    assert r["findings"][0]["severity"] == "critical"
    assert decide({"code-reviewer": r})["decision"] == "NO-GO"


def test_the_twelve_kept_findings_are_the_most_severe_ones():
    items = [finding("low", i) for i in range(12)] + [finding("critical", 99)]
    r = method.parse_persona("code-reviewer", {"findings": items}, {"a.py"})
    assert len(r["findings"]) == 12 and r["findings"][0]["severity"] == "critical"


def test_a_dot_slash_path_still_matches_the_diff():
    r = method.parse_persona("code-reviewer", {"findings": [finding(file="./a.py")]}, {"a.py"})
    assert [f["file"] for f in r["findings"]] == ["a.py"] and r["dropped"] == 0


def test_removing_an_auth_guard_is_not_a_small_change():
    guard = "--- a/views.py\n+++ b/views.py\n@@ -1,3 +1,2 @@\n-@login_required\n def home(r):\n     return 1\n"
    assert method.is_small(["views.py"], 1, guard) is False
    plain = "--- a/util.py\n+++ b/util.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    assert method.is_small(["util.py"], 2, plain) is True


def test_an_empty_merged_diff_is_no_go():
    out = decide({}, files=[])
    assert out["decision"] == "NO-GO" and any("rỗng" in b for b in out["blockers"])


def test_a_high_review_finding_with_a_line_blocks_and_without_one_only_recommends():
    with_line = {"review": [{"file": "a.py", "line": "12", "severity": "high", "problem": "p", "fix": "f"}]}
    out = decide({}, review=with_line)
    assert out["decision"] == "NO-GO" and any(b.startswith("review:") for b in out["blockers"])
    no_line = {"review": [{"file": "a.py", "line": "", "severity": "high", "problem": "p", "fix": "f"}]}
    out = decide({}, review=no_line)
    assert out["decision"] == "GO" and any(r.startswith("review:") for r in out["recommended"])


@pytest.mark.parametrize("answer", [{}, {"issues": [{"file": "a.py", "line": 3, "severity": "high"}]}, {"findings": []}, {"review": "none"}, {"review": ["a.py: bad"]}])
def test_a_review_answer_that_is_not_the_review_schema_is_not_run(answer):
    r = method.parse_review(answer, {"a.py"})
    assert r["status"] == "not_run" and "review" not in r


def test_an_empty_review_list_is_a_clean_answer():
    r = method.parse_review({"review": []}, {"a.py"})
    assert r["status"] == "ok" and r["review"] == []


@pytest.mark.parametrize("severity", ["critical", "urgent", "", None])
def test_an_unknown_review_severity_counts_as_high(severity):
    item = {"file": "a.py", "line": 3, "severity": severity, "problem": "p", "fix": "f"}
    assert method.parse_review({"review": [item]}, {"a.py"})["review"][0]["severity"] == "high"


def test_a_review_that_did_not_run_is_recommended_not_silent():
    out = decide({}, review={"status": "not_run", "error": "the answer is not the review schema"})
    assert out["decision"] == "GO" and any(r.startswith("review: không chạy") for r in out["recommended"])
    assert not any(r.startswith("review:") for r in decide({}, review={"status": "ok", "review": []})["recommended"])
