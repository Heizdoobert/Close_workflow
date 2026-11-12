"""Security hardening and code-verify scans: unit cases on the added lines, the per-step gate, and a step that writes sloppy code being told what to fix."""

import json
import subprocess
import sys

import pytest

from flow_engine import audit, gate, plan
from test_plan import env, step  # noqa: F401  (env is the shared fake-agent fixture)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake binaries are POSIX scripts")


def lines(f, *texts):
    return [(f, i, t) for i, t in enumerate(texts, 1)]


# --- security hardening ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,rule", [
    ("x = ev" + "al(data)", "eval"), ("subprocess.run(cmd, shell=" + "True)", "shell"), ("os.sys" + "tem(cmd)", "shell"), ("obj = pickle.lo" + "ads(blob)", "deserialisation"),
    ("cfg = yaml.lo" + "ad(open(p))", "deserialisation"), ("requests.get(u, verify=" + "False)", "TLS"), ('cur.execute(f"SELECT * FROM t WHERE id = {uid}")', "SQL"),
    ('cur.execute("SELECT * FROM t WHERE id = " + uid)', "SQL"), ("p = tempfile.mkt" + "emp()", "temp"), ("os.chmod(p, 0o77" + "7)", "world-writable"),
    ("resp.set_cookie(k, v, httponly=" + "False)", "cookie"), ("el.inner" + "HTML = html", "XSS"), ("{{ body|sa" + "fe }}", "XSS")])
def test_dangerous_patterns_are_hard_findings(text, rule):
    res = audit.hardening(lines("app.py", text), ["app.py"])
    assert res["hard"] and rule in res["hard"][0], res


@pytest.mark.parametrize("text", ['cur.execute("SELECT * FROM t WHERE id = %s", (uid,))', "cfg = yaml.safe_load(fh)", "subprocess.run(['ls', d])", "print('select a file from the list')",
                                  "x = evaluate(data)", "re.compile(r'a').exec_body", "r = requests.get(u, timeout=5)"])
def test_safe_code_is_not_flagged(text):
    assert audit.hardening(lines("app.py", text), ["app.py"]) == {"hard": [], "soft": []}


def test_tests_and_docs_are_not_scanned_for_hardening():
    bait = "x = ev" + "al(data)"
    assert audit.hardening(lines("tests/test_x.py", bait) + lines("README.md", bait), []) == {"hard": [], "soft": []}


def test_soft_findings_and_dependency_changes_are_reported_not_blocking():
    res = audit.hardening(lines("app.py", "shutil.rmtree(target)", "h = hashlib.md5(b)", "app.run(host='0.0.0.0')") , ["app.py", "requirements.txt"])
    assert not res["hard"] and len(res["soft"]) == 4 and any("pip-audit" in s for s in res["soft"])


def test_findings_never_echo_the_matched_text():
    secret_ish = "x = ev" + "al(PASSWORD_1234)"
    assert "PASSWORD_1234" not in json.dumps(audit.hardening(lines("app.py", secret_ish), []))


# --- code verify -----------------------------------------------------------------------------------------------------

def _verify(tmp_path, name, src, declared=(), deleted=()):
    (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / name).write_text(src, encoding="utf-8")
    added = [(name, i, t) for i, t in enumerate(src.splitlines(), 1)]
    return audit.verify(str(tmp_path), added, set(declared), set(deleted))


def test_stub_function_is_a_hard_finding(tmp_path):
    res = _verify(tmp_path, "m.py", "def work(x):\n    pass\n\n\ndef later():\n    raise NotImplementedError\n\n\ndef real():\n    return 1\n")
    assert sorted(h.split(" ")[0] for h in res["hard"]) == ["m.py:1", "m.py:5"] and all("stub" in h for h in res["hard"])


def test_abstract_and_protocol_methods_are_allowed_to_be_empty(tmp_path):
    src = "import abc\n\n\nclass A(abc.ABC):\n    @abc.abstractmethod\n    def f(self):\n        pass\n\n\nclass P(Protocol):\n    def g(self): ...\n"
    assert _verify(tmp_path, "m.py", src)["hard"] == []


def test_swallowed_exceptions_and_debug_leftovers(tmp_path):
    src = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n    try:\n        g()\n    except:\n        log()\n    breakp" + "oint()\n"
    res = _verify(tmp_path, "m.py", src)["hard"]
    assert len(res) == 3 and any("swallows" in h for h in res) and any("bare except" in h for h in res) and any("debugger" in h for h in res)


def test_narrow_except_pass_is_fine(tmp_path):
    assert _verify(tmp_path, "m.py", "def f():\n    try:\n        g()\n    except OSError:\n        pass\n")["hard"] == []


def test_a_test_without_an_assertion_is_hard_but_raises_counts(tmp_path):
    src = "import pytest\n\n\ndef test_a():\n    f()\n\n\ndef test_b():\n    with pytest.raises(ValueError):\n        f()\n\n\ndef test_c():\n    assert f() == 1\n"
    res = _verify(tmp_path, "tests/test_m.py", src)["hard"]
    assert len(res) == 1 and "test_m.py:4" in res[0]


def test_a_test_file_wrapped_in_a_docstring_defines_no_test(tmp_path):
    wrapped = '"""Test the evaluator.\n\ndef test_a():\n    assert f() == 1\n"""\n'
    assert any("defines no test" in h for h in _verify(tmp_path, "tests/test_m.py", wrapped)["hard"])
    assert _verify(tmp_path, "tests/test_ok.py", "def test_a():\n    assert f() == 1\n")["hard"] == []
    assert _verify(tmp_path, "tests/test_cls.py", "class TestA:\n    def test_a(self):\n        assert f()\n")["hard"] == []
    assert _verify(tmp_path, "tests/conftest.py", "import pytest\n\n\n@pytest.fixture\ndef x():\n    return 1\n")["hard"] == []  # helper files hold no tests


def test_soft_slop_is_reported(tmp_path):
    src = "import os\nimport sys\n\n# TO" + "DO later\n# def old():\n#     return 1\nprint(sys.argv)\n"
    soft = _verify(tmp_path, "m.py", src)["soft"]
    assert any("unused import os" in s for s in soft) and any("unfinished" in s for s in soft) and any("commented-out" in s for s in soft)
    assert not any("sys" in s and "unused" in s for s in soft)


def test_declared_files_must_exist_and_hold_code(tmp_path):
    (tmp_path / "empty.py").write_text("# nothing\n")
    (tmp_path / "blank.txt").write_text("")
    res = audit.verify(str(tmp_path), [], {"missing.py", "empty.py", "blank.txt", "gone.py"}, {"gone.py"})["hard"]
    assert sorted(h.split(" ")[0] for h in res) == ["blank.txt", "empty.py", "missing.py"]


def test_doc_claims_must_match_the_tree(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "requirements.txt").write_text("airtest==1.3.6\nmcp==1.28.0\n")
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n")
    doc = ["`a.py:2` is real, `a.py:9` is past the end, `gone.py:1` is missing", "Pinned: airtest==1.3.6 and Mcp==1.28.0, but airtest==1.2.3 is invented", "see http://x.io/a.py:80 and v1.2.3"]
    res = audit.claims(str(tmp_path), [("docs/r.md", i, t) for i, t in enumerate(doc, 1)])
    assert sorted(h.split(" ", 1)[1] for h in res) == ["cites a.py:9 but the file has 2 lines", "cites gone.py:1 but that file does not exist", "claims pin airtest==1.2.3 but no manifest in the tree has it"]
    assert audit.claims(str(tmp_path), [("a.py", 1, "airtest==9.9 gone.py:1")]) == []  # only Markdown is read


def test_a_step_that_writes_an_invented_pin_is_sent_back(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "requirements.txt").write_text("airtest==1.3.6\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "i")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "r.md").write_text("## Deps\nAll pinned, e.g. airtest==1.2.3\n")
    g = gate.run_gates(tmp_path, allowed=["docs/r.md"])
    assert not g["ok"] and "airtest==1.2.3" in g["gates"]["code_verify"]["findings"][0]


# --- as gates in a plan step -----------------------------------------------------------------------------------------

def _git(root, *a):
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", *a], cwd=root, check=True, capture_output=True)


def test_run_gates_runs_security_and_code_verify_for_a_step_only(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("X = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "i")
    (tmp_path / "a.py").write_text("X = 1\n\n\ndef f(c):\n    return ev" + "al(c)\n\n\ndef g():\n    pass\n")
    assert {"security", "code_verify"}.isdisjoint(gate.run_gates(tmp_path)["gates"])
    g = gate.run_gates(tmp_path, allowed=["a.py"])
    assert not g["ok"] and not g["gates"]["security"]["ok"] and not g["gates"]["code_verify"]["ok"]
    assert "a.py:5" in g["gates"]["security"]["findings"][0] and "a.py:8" in g["gates"]["code_verify"]["findings"][0]


def test_adding_a_nosec_marker_is_a_weakened_bar(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("X = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "i")
    (tmp_path / "a.py").write_text("X = 1\nY = run(c)  # no" + "sec\n")
    assert not gate.constraints(tmp_path)["ok"]


def test_a_sloppy_step_is_sent_back_with_the_findings_and_then_passes(env, monkeypatch):
    seen = []
    real = plan.agents.call
    monkeypatch.setattr(plan.agents, "call", lambda chain, prompt, o, t: (seen.append(prompt), real(chain, prompt, o, t))[1])
    env.script([step("s1", files=["a.py"], acc="python3 -c 'import a; assert a.f(2) == 4'")],
               {"s1": {"files": {"a.py": "def f(x):\n    pass\n"}, "fixed_files": {"a.py": "def f(x):\n    return x * 2\n"}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"
    retry = next(p for p in seen if "did not pass the automated checks" in p)
    assert "Findings:" in retry and "code_verify" in retry and "a.py:1" in retry
    st = json.loads((env.home / "plans" / pid / "state.json").read_text(encoding="utf-8"))
    assert st["steps"]["s1"]["attempts"][0]["gates_ok"] is False and st["security"]["status"] == "ok" and st["verify"]["n_hard"] == 0


def test_hard_findings_from_the_whole_diff_make_the_ship_decision_no_go(env):
    env.script([step("s1", files=["auth.py"], acc="python3 -c 'import auth'")], {"s1": {"files": {"auth.py": "import os\n\n\ndef run(c):\n    return 1\n"}}})
    pid = plan.create("g")
    d = env.home / "plans" / pid
    assert plan.approve(pid, wait_for=True) == "done"
    st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    st["security"] = {"status": "ok", "hard": ["auth.py:9 eval/exec of a string"], "soft": [], "n_hard": 1, "n_soft": 0}  # what a scan of a worse diff would hold
    from flow_engine import method
    out = method.decide(plan.load(pid)[1], st, {}, ["auth.py"], False, None)
    assert out["decision"] == "NO-GO" and any("hardening" in b and "eval/exec" in b for b in out["blockers"])
