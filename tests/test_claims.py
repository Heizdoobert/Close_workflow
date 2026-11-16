"""Claims check: analysis.md is written, research files are real, and the claimed PASS/FAIL is compared with a rerun of the acceptance."""

import os

import pytest

from flow_engine import claims

ANALYSIS = {
    "research": [{"file": "README.md", "finding": "x"}],
    "root_cause": "r",
    "approach": "a",
    "alternatives": [],
    "fix": "f",
    "blast_radius": {"affected": [], "risks": "none"},
    "spec": "s",
}

HEADINGS = ["# Phân tích (analysis)", "## Research", "## Root cause", "## Approach", "## Alternatives", "## Fix", "## Blast radius", "## Spec"]


def full_analysis():
    return {
        "research": [{"file": "flow_engine/plan.py", "finding": "create writes plan.json"}],
        "root_cause": "the report trusts the agent",
        "approach": "compare with a rerun",
        "alternatives": [{"option": "ask the model again", "rejected_because": "same model, same blind spot"}],
        "fix": "claims.machine_check",
        "blast_radius": {"affected": ["flow_engine/plan.py"], "risks": "a false block on a true 90% pass"},
        "spec": "PASS and FAIL are compared",
    }


def test_write_analysis_has_every_heading(tmp_path):
    path = claims.write_analysis(tmp_path, full_analysis())
    assert path == tmp_path / "analysis.md"
    text = path.read_text(encoding="utf-8")
    for heading in HEADINGS:
        assert heading in text
    assert "- `flow_engine/plan.py`: create writes plan.json" in text
    assert not list(tmp_path.glob(".analysis-*"))


def test_write_analysis_redacts_secret_shapes(tmp_path):
    key = "".join(["sk", "-ant", "-", "Q1w2E3r4T5y6U7i8O9p0"])
    analysis = full_analysis()
    analysis["fix"] = f"use {key} for the call"
    text = claims.write_analysis(tmp_path, analysis).read_text(encoding="utf-8")
    assert key not in text
    assert "[REDACTED]" in text


def test_falsy_analysis_writes_nothing(tmp_path):
    assert claims.write_analysis(tmp_path, None) is None
    assert claims.write_analysis(tmp_path, {}) is None
    assert claims.write_analysis(tmp_path, "not a dict") is None
    assert list(tmp_path.iterdir()) == []


def test_check_research_flags_bad_paths_and_accepts_real_file(tmp_path):
    (tmp_path / "real.py").write_text("x = 1\n")
    analysis = {"research": [
        {"file": "/etc/passwd", "finding": "x"},
        {"file": "../x", "finding": "x"},
        {"file": "missing.py", "finding": "x"},
        {"file": "real.py", "finding": "x"},
    ]}
    problems = claims.check_research(analysis, tmp_path)
    assert problems == ["research không căn cứ: /etc/passwd", "research không căn cứ: ../x", "research không căn cứ: missing.py"]


def research_of(file):
    return {"research": [{"file": file, "finding": "x"}]}


def test_drive_letter_path_is_rejected(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n")
    assert len(claims.check_research(research_of("C:/x.py"), tmp_path)) == 1
    assert len(claims.check_research(research_of("C:x.py"), tmp_path)) == 1


def test_backslash_path_is_rejected(tmp_path):
    (tmp_path / "flow_engine").mkdir()
    (tmp_path / "flow_engine" / "claims.py").write_text("x = 1\n")
    assert len(claims.check_research(research_of("flow_engine\\claims.py"), tmp_path)) == 1


def test_dotdot_and_absolute_are_rejected(tmp_path):
    assert len(claims.check_research(research_of("../x"), tmp_path)) == 1
    assert len(claims.check_research(research_of("/etc/passwd"), tmp_path)) == 1


def test_symlink_to_outside_file_is_rejected(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    try:
        os.symlink(outside, wt / "link.txt")
    except OSError:
        pytest.skip("symlinks are not available here")
    assert len(claims.check_research(research_of("link.txt"), wt)) == 1


def test_real_file_is_accepted(tmp_path):
    (tmp_path / "README.md").write_text("hello\n")
    assert claims.check_research(research_of("README.md"), tmp_path) == []


def test_check_analysis_ok_when_untouched(tmp_path):
    claims.write_analysis(tmp_path, ANALYSIS)
    assert claims.check_analysis(tmp_path, {}, ANALYSIS) == []


def test_check_analysis_detects_an_edited_file(tmp_path):
    claims.write_analysis(tmp_path, ANALYSIS)
    (tmp_path / "analysis.md").write_text("changed", encoding="utf-8")
    problems = claims.check_analysis(tmp_path, {}, ANALYSIS)
    assert len(problems) == 1
    assert "analysis.md" in problems[0]


def test_check_analysis_uses_state_hash_first(tmp_path):
    claims.write_analysis(tmp_path, ANALYSIS)
    problems = claims.check_analysis(tmp_path, {"analysis_sha": "deadbeef"}, ANALYSIS)
    assert len(problems) == 1


def test_analysis_text_is_redacted():
    key = "".join(["sk", "-ant", "-", "Q1w2E3r4T5y6U7i8O9p0"])
    analysis = dict(ANALYSIS, root_cause=f"leaked {key} here")
    text = claims.analysis_text(analysis)
    assert key not in text
    assert "[REDACTED]" in text


def make_plan(*statuses):
    steps = [{"id": f"s{i}", "acceptance": "python3 -m pytest -q"} for i in range(1, len(statuses) + 1)]
    state = {"steps": {f"s{i}": {"status": st} for i, st in enumerate(statuses, 1)}}
    return {"steps": steps}, state


def accept_with(ok):
    return lambda step, wt: {"ok": ok}


def test_matching_claim_has_match_true(tmp_path):
    plan, state = make_plan("done")
    res = claims.machine_check(plan, state, accept_with(True), tmp_path, {"s1": "PASS"})
    assert res["rows"] == [{"step": "s1", "claimed": "PASS", "actual": True, "match": True}]
    assert res["ok"] is True and res["blockers"] == []


def test_pass_claim_with_failing_acceptance_is_blocker(tmp_path):
    plan, state = make_plan("done")
    res = claims.machine_check(plan, state, accept_with(False), tmp_path, {"s1": "PASS"})
    assert res["ok"] is False
    assert res["rows"][0]["match"] is False
    assert res["blockers"] == ["claims: bước s1 khai PASS nhưng acceptance chạy lại cho FAIL"]


def test_fail_claim_with_passing_acceptance_is_blocker(tmp_path):
    plan, state = make_plan("done")
    res = claims.machine_check(plan, state, accept_with(True), tmp_path, {"s1": "FAIL"})
    assert res["ok"] is False
    assert res["blockers"] == ["claims: bước s1 khai FAIL nhưng acceptance chạy lại cho PASS"]


def test_no_claim_gives_no_match_and_no_blocker(tmp_path):
    plan, state = make_plan("done")
    res = claims.machine_check(plan, state, accept_with(False), tmp_path, None)
    assert res["rows"][0]["claimed"] is None
    assert res["rows"][0]["match"] is None
    assert res["ok"] is True


def test_unknown_claim_value_becomes_no_claim(tmp_path):
    plan, state = make_plan("done")
    res = claims.machine_check(plan, state, accept_with(False), tmp_path, {"s1": "maybe"})
    assert res["rows"][0]["claimed"] is None
    assert res["ok"] is True


def test_pending_step_is_skipped(tmp_path):
    plan, state = make_plan("pending")
    res = claims.machine_check(plan, state, accept_with(False), tmp_path, {"s1": "PASS"})
    assert res["rows"] == []
    assert res["ok"] is True
