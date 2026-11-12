"""The 16-step method: analysis block (research..spec), blast-radius gate, review + simplify pass, coverage table in the report, SHIP decision. Interview, security and code-verify have their own test files."""

import json
import shutil
import subprocess
import sys

import pytest

from flow_engine import gate, method, plan
from test_plan import env, step  # noqa: F401  (env is the shared fake-agent fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")
GOOD = {"research": [{"file": "README.md", "finding": "the repo only holds a readme"}], "root_cause": "nothing implements the goal yet",
        "approach": "add the files the steps list, one module per step", "alternatives": [{"option": "one big file", "rejected_because": "no parallel steps"}],
        "fix": "small modules, each checked by its own acceptance command", "blast_radius": {"affected": ["README.md"], "risks": "none: only new files are added"},
        "spec": "each step writes its file; the acceptance command imports it"}
EXISTS = {"README.md"}.__contains__


def _read(env, pid, name):
    return (env.home / "plans" / pid / name).read_text(encoding="utf-8")


# --- steps 1-6: the analysis block -----------------------------------------------------------------------------------

def test_a_complete_analysis_has_no_problems():
    assert method.problems(GOOD, EXISTS) == []


@pytest.mark.parametrize("key", ["root_cause", "approach", "fix", "spec", "research", "alternatives", "blast_radius"])
def test_each_missing_part_is_named(key):
    bad = {k: v for k, v in GOOD.items() if k != key}
    assert any(key in p for p in method.problems(bad, EXISTS))


def test_research_must_cite_a_file_that_exists():
    fake = {**GOOD, "research": [{"file": "no/such.py", "finding": "an invented finding about nothing"}]}
    assert any("no/such.py" in p for p in method.problems(fake, EXISTS))
    assert method.problems({**GOOD, "research": [{"file": "./README.md", "finding": "found it in the readme"}]}, EXISTS) == []


def test_no_analysis_at_all_is_one_clear_problem():
    assert len(method.problems(None, EXISTS)) == 1


def test_create_asks_again_then_stores_the_analysis(env, monkeypatch):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    monkeypatch.setenv("FAKE_NO_ANALYSIS", "1")
    with pytest.raises(plan.PlanError, match="planner did not produce a valid plan"):
        plan.create("g")
    monkeypatch.delenv("FAKE_NO_ANALYSIS")
    pid = plan.create("g")
    stored = json.loads(_read(env, pid, "plan.json"))["analysis"]
    assert stored["root_cause"] and stored["blast_radius"]["affected"] == ["README.md"]
    shown = plan.show(pid)
    assert "root cause" in shown and "do s1" in shown  # the analysis does not push the steps out of the review text


def _planner_replies(env, monkeypatch, replies):
    """The planner agent answers `replies` one call at a time (last one repeats); returns the prompts it was given."""
    prompts = []

    def fake(chain, prompt, o, trace=None):
        prompts.append(prompt)
        return plan.agents.Result(True, replies[min(len(prompts) - 1, len(replies) - 1)]), 0.0, {}
    monkeypatch.setattr(plan.agents, "call", fake)
    return prompts


def test_planner_gets_several_repair_rounds_and_a_raw_newline_in_a_string_is_fine(env, monkeypatch):
    good = json.dumps({"analysis": GOOD, "steps": [step("s1", files=["a.py"], acc="python3 -c 'import a'")]})
    lenient = good.replace("do s1", "do\ns1")  # a raw newline inside a JSON string: strict json.loads rejects it
    assert lenient != good
    nostep = json.dumps({"analysis": GOOD, "steps": [{k: v for k, v in step("s1", files=["a.py"], acc="true").items() if k != "prompt"}]})
    prompts = _planner_replies(env, monkeypatch, ["not json", nostep, "{broken", lenient])
    pid = plan.create("g")
    assert len(prompts) == 4 and "was rejected" in prompts[1] and "missing prompt" in prompts[2]
    assert prompts[3].count("was rejected") == 1  # only the last rejection is repeated, the prompt does not grow
    assert json.loads(_read(env, pid, "plan.json"))["steps"][0]["id"] == "s1"


def test_planner_gives_up_after_the_round_limit_and_says_why(env, monkeypatch):
    monkeypatch.setenv("FLOW_PLAN_ROUNDS", "2")
    prompts = _planner_replies(env, monkeypatch, ["still not json"])
    with pytest.raises(plan.PlanError, match="did not produce a valid plan: .*no JSON"):
        plan.create("g")
    assert len(prompts) == 2


def test_step_agents_are_given_the_analysis_and_the_no_rename_rule(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    p = next(x for x in seen if "Your step [" in x)
    assert "Plan analysis (follow it)" in p and "small modules" in p and "Do not rename, move or delete" in p


# --- step 5: blast radius gate ---------------------------------------------------------------------------------------

def _git(root, *a):
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", *a], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "lib.py").write_text("def helper_one():\n    return 1\n\n\nclass Thing:\n    pass\n")
    (tmp_path / "use.py").write_text("from lib import helper_one\n\nprint(helper_one())\n")
    (tmp_path / "old.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_untouched_tree_and_declared_changes_pass(repo):
    assert gate.blast_radius(repo, [])["ok"]
    (repo / "old.txt").unlink()
    assert not gate.blast_radius(repo, [])["ok"]
    assert gate.blast_radius(repo, ["old.txt"])["ok"]  # declared in the step's files


def test_undeclared_rename_is_caught(repo):
    _git(repo, "mv", "old.txt", "new.txt")
    res = gate.blast_radius(repo, ["new.txt"])
    assert not res["ok"] and "old.txt" in res["findings"][0]


def test_removing_a_function_that_is_still_used_is_caught(repo):
    (repo / "lib.py").write_text("class Thing:\n    pass\n")
    res = gate.blast_radius(repo, ["lib.py"])
    assert not res["ok"] and "helper_one" in res["findings"][0] and "use.py" in res["findings"][0]


def test_moving_a_function_to_a_new_file_is_fine(repo):
    (repo / "lib.py").write_text("class Thing:\n    pass\n")
    (repo / "helpers.py").write_text("def helper_one():\n    return 1\n")
    assert gate.blast_radius(repo, ["lib.py", "helpers.py"])["ok"]


def test_removing_an_unused_function_is_fine(repo):
    (repo / "use.py").write_text("print(1)\n")
    (repo / "lib.py").write_text("class Thing:\n    pass\n")
    assert gate.blast_radius(repo, ["lib.py", "use.py"])["ok"]


def test_run_gates_runs_it_only_for_a_plan_step(repo):
    assert "blast_radius" not in gate.run_gates(repo)["gates"]
    assert gate.run_gates(repo, allowed=[])["gates"]["blast_radius"]["ok"]


# --- steps 10-12: review, simplify, report ---------------------------------------------------------------------------

REVIEW = {"review": [{"file": "a.py", "line": 1, "severity": "HIGH", "problem": "no guard for a negative value", "fix": "reject it"},
                     {"file": "not_in_diff.py", "line": 3, "severity": "high", "problem": "invented", "fix": "none"}],
          "simplify": [{"file": "a.py", "what": "inline the constant"}], "edge_cases": ["empty input", "negative value"]}


def _run_plan(env, monkeypatch, review=None):
    if review is not None:
        monkeypatch.setenv("FAKE_REVIEW", review)
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    return pid


def test_review_findings_are_kept_only_for_files_in_the_diff(env, monkeypatch):
    pid = _run_plan(env, monkeypatch, json.dumps(REVIEW))
    rv = json.loads(_read(env, pid, "state.json"))["review"]
    assert rv["status"] == "ok" and [r["file"] for r in rv["review"]] == ["a.py"] and rv["review"][0]["severity"] == "high" and rv["dropped"] == 1
    rep = _read(env, pid, "report.md")
    for needle in ("Độ phủ 16 bước", "no guard for a negative value", "inline the constant", "negative value", "Phân tích (nghiên cứu", "nguyên nhân gốc", "blast_radius qua ở 1/1"):
        assert needle in rep, needle
    assert "invented" not in rep


def test_the_diff_sent_to_review_and_ship_has_its_key_shaped_secrets_masked(env, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "gitleaks" else real_which(name, *a, **k))  # the test must not depend on gitleaks being installed
    key = "".join(["AKIA", "ABCDEFGHIJKLMNOP"])  # built at runtime: no key-shaped literal in this file. Already in the repo, so only the diff's context lines carry it
    (env.repo / "cfg.py").write_text(f'KEY = "{key}"\nA = 1\n')
    _git(env.repo, "add", "-A")
    _git(env.repo, "commit", "-q", "-m", "cfg")
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    env.script([step("s1", files=["cfg.py", "b.py", "c.py"], acc="python3 -c 'import cfg, b, c'")],  # three files: a smaller change skips the SHIP fan-out
               {"s1": {"files": {"cfg.py": f'KEY = "{key}"\nA = 2\n', "b.py": "B = 1\n", "c.py": "C = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    diffs = [p for p in seen if "A = 2" in p and "+++ b/cfg.py" in p]
    assert len(diffs) >= 2  # the review and at least one SHIP persona saw the diff
    assert all(key not in p and "[REDACTED]" in p for p in diffs) and not [p for p in seen if key in p]


def test_all_sixteen_steps_appear_in_the_coverage_table(env, monkeypatch):
    pid = _run_plan(env, monkeypatch)
    rows = [l for l in _read(env, pid, "report.md").splitlines() if l.startswith("| ") and l.split("|")[1].strip().isdigit()]
    assert [l.split("|")[1].strip() for l in rows] == [str(i) for i in range(1, 17)]
    assert "THIẾU" in rows[0] and all("THIẾU" not in l for l in rows[1:])  # no interview was done here, everything else ran


def test_an_unusable_review_reply_does_not_change_the_plan_result(env, monkeypatch):
    pid = _run_plan(env, monkeypatch, "not json at all")
    st = json.loads(_read(env, pid, "state.json"))
    assert st["status"] == "done" and st["review"]["status"] == "not_run"
    assert "reply tail: 'not json at all'" in st["review"]["error"]  # the failure can be read afterwards
    assert len([c for c in env.calls() if c[0] == "review"]) == 2  # one retry for an unparsable reply
    rows = [l for l in _read(env, pid, "report.md").splitlines() if l.startswith("| 12 ") or l.startswith("| 13 ")]  # review and simplify
    assert len(rows) == 2 and all("THIẾU" in r for r in rows)  # the report says the step did not run


def test_review_can_be_switched_off(env, monkeypatch):
    monkeypatch.setenv("FLOW_REVIEW", "0")
    pid = _run_plan(env, monkeypatch)
    assert json.loads(_read(env, pid, "state.json"))["review"] == {"status": "off"}
    assert not [c for c in env.calls() if c[0] == "review"]


def test_a_plan_without_analysis_still_runs_and_the_report_says_what_was_not_checked(env, monkeypatch):
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a'")], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    d = env.home / "plans" / pid
    p = json.loads((d / "plan.json").read_text(encoding="utf-8"))
    del p["analysis"]  # an older or hand-written plan
    (d / "plan.json").write_text(json.dumps(p), encoding="utf-8")
    assert plan.approve(pid, wait_for=True) == "done"
    rep = _read(env, pid, "report.md")
    assert "không có khối analysis" in rep and "plan không có khối analysis" in rep


# --- plans/ on a filesystem that cannot keep file modes (real run: repo on ext4, FLOW_SKILL on NTFS through FUSE) -------

def test_nomode_makes_git_ignore_mode_changes_only_when_the_filesystem_loses_them(env, monkeypatch, tmp_path):
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv(k, "0")  # so the test's own changes are undone afterwards
    monkeypatch.delenv("GIT_CONFIG_COUNT")
    monkeypatch.setattr(plan, "_mode_lost", lambda p: False)
    plan._nomode()
    assert "GIT_CONFIG_COUNT" not in plan.os.environ
    monkeypatch.setattr(plan, "_mode_lost", lambda p: True)
    plan._nomode()
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "i")
    (tmp_path / "a.txt").chmod(0o755)  # what the lossy filesystem does to every file
    assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout == ""
    assert plan.os.environ["GIT_CONFIG_COUNT"] == "1"
    plan._nomode()  # idempotent per process: a second call must not stack another entry
    assert plan.os.environ["GIT_CONFIG_COUNT"] == "1"


def test_a_key_written_by_the_acceptance_command_is_caught_before_the_commit(env, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "gitleaks" else real_which(name, *a, **k))  # the built-in scan, whatever is installed
    acc = "python3 -c \"open('leak.txt', 'w').write(''.join(['gh', 'p_', 'a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8']))\" && test -f a.py"  # the key exists only after the command ran
    env.script([step("s1", files=["a.py"], acc=acc)], {"s1": {"files": {"a.py": "A = 1\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) != "done"
    st = json.loads(_read(env, pid, "state.json"))
    assert st["steps"]["s1"]["status"] == "failed" and "secrets:FAIL" in st["steps"]["s1"]["attempts"][0]["gates"]
    assert "p_a1B2c3D4" not in json.dumps(st)  # and the key itself is not echoed into the state
