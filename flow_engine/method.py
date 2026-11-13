"""The 16-step working method, as far as code can hold it.

 1 interview (interview.py)  -> `brief` of plan.json, confirmed by the user
 2 research  3 why (root cause)  4 how (approach + rejected alternatives)  5 fix  6 blast radius  7 spec  -> `analysis` block of plan.json
 8 plan  9 build  10 test            -> plan.py steps, gate.py, acceptance
 11 security hardening, 14 code-verify -> audit.py: per-step gates and one scan of the whole diff
 12 review, 13 simplify              -> one read-only agent call over the merged diff
 15 report                           -> report.md with a coverage table
 16 ship (the agent-skills /ship): three read-only personas in parallel (code-reviewer, security-auditor, test-engineer), merged by code into GO / NO-GO + rollback plan

What the harness checks itself: research cites files that exist at the base commit, every analysis field is present, a step may not delete, rename
or orphan anything it did not declare (gate.blast_radius), review findings must point at files that are in the diff. What it cannot check: that the
root cause or the spec is true. The coverage table says which is which.
"""

import re
from pathlib import Path

TEXT_MIN = 20
SHAPE = ('"analysis":{"research":[{"file":"path that exists in the repo","finding":"what you learned there"}],"root_cause":"why the problem or need exists",'
         '"approach":"how it can be done","alternatives":[{"option":"another way","rejected_because":"why not"}],"fix":"the chosen solution and why it is the best",'
         '"blast_radius":{"affected":["paths or modules that call or depend on what changes"],"risks":"what could break and how the plan avoids it"},'
         '"spec":"detailed technical spec: behaviour, interfaces, edge cases"}')
RULES = ("Work in this order: 1 research the repo (cite real files in analysis.research), 2 find the root cause, 3 how it can be done (approach, and at least one "
         "rejected alternative), 4 the chosen fix, 5 blast radius (what calls or depends on what you change: no existing path or public name may change unless a "
         "step lists it in its files), 6 spec. Then 7 plan the steps.")
SIMPLE = ("root_cause", "approach", "fix", "spec")
CUT = 1500


def _s(x, n=CUT):
    return str(x or "").strip()[:n]


def problems(a, has_file):
    """What is missing or weak in an analysis block (empty list = fine). has_file(path) says whether a cited file exists at the base commit."""
    if not isinstance(a, dict):
        return ["analysis block missing (research, root_cause, approach, alternatives, fix, blast_radius, spec)"]
    out = [f"analysis.{k} missing or shorter than {TEXT_MIN} chars" for k in SIMPLE if len(_s(a.get(k))) < TEXT_MIN]
    rs = [r for r in (a.get("research") or []) if isinstance(r, dict)]
    if not any(len(_s(r.get("finding"))) >= 10 and has_file(re.sub(r"^\./", "", _s(r.get("file")))) for r in rs):
        out.append("analysis.research needs at least one {file, finding} where the file exists in the repo: " + (f"not found: {[_s(r.get('file'), 80) for r in rs][:3]}" if rs else "none given"))
    if not any(isinstance(x, dict) and _s(x.get("option")) and _s(x.get("rejected_because")) for x in (a.get("alternatives") or [])):
        out.append("analysis.alternatives needs at least one {option, rejected_because}")
    b = a.get("blast_radius")
    if not isinstance(b, dict) or not isinstance(b.get("affected"), list) or len(_s(b.get("risks"))) < 10:
        out.append("analysis.blast_radius needs affected (list, may be empty for brand-new files) and risks (text)")
    return out


def clean(a):
    """Only the known keys, strings cut, so plan.json stays small and predictable."""
    b = a["blast_radius"]
    return {"research": [{"file": _s(r.get("file"), 200), "finding": _s(r.get("finding"), 300)} for r in a["research"] if isinstance(r, dict)][:8],
            **{k: _s(a.get(k)) for k in SIMPLE},
            "alternatives": [{"option": _s(x.get("option"), 300), "rejected_because": _s(x.get("rejected_because"), 300)} for x in a["alternatives"] if isinstance(x, dict)][:5],
            "blast_radius": {"affected": [_s(x, 200) for x in b["affected"]][:12], "risks": _s(b.get("risks"), 500)}}


def _cut(text, n):
    return text.encode()[:n].decode(errors="ignore")


def brief(a, limit=1100):
    """What each step agent is told of the analysis: the chosen approach, the blast radius and the spec, within `limit` bytes."""
    if not a:
        return ""
    t = (f"Plan analysis (follow it):\nfix: {_cut(a['fix'], 250)}\naffected, must keep working: {_cut('; '.join(a['blast_radius']['affected']) or 'nothing existing', 250)}\n"
         f"spec: {_cut(a['spec'], 450)}\n")
    return _cut(t, limit) + "\n"


def md(a, each=120):
    """Compact form for plan.md (the full text stays in plan.json)."""
    if not a:
        return []
    return ["Analysis (full text in plan.json):", f"- research: {', '.join(r['file'] for r in a['research'])}", f"- root cause: {_cut(a['root_cause'], each)}",
            f"- approach: {_cut(a['approach'], each)} | rejected: {_cut('; '.join(x['option'] for x in a['alternatives']), each)}", f"- fix: {_cut(a['fix'], each)}",
            f"- blast radius: {_cut('; '.join(a['blast_radius']['affected']) or '-', each)} | risks: {_cut(a['blast_radius']['risks'], each)}", f"- spec: {_cut(a['spec'], each)}", ""]


def report_md(a):
    """The analysis in full, for the top of report.md (Vietnamese labels like the rest of the report)."""
    if not a:
        return ["_Plan này không có khối analysis (plan cũ hoặc viết tay): bước 1-6 không được kiểm._"]
    return (["**1. Nghiên cứu (file có thật ở base commit):**"] + [f"- `{r['file']}`: {r['finding']}" for r in a["research"]]
            + [f"**2. Nguyên nhân gốc:** {a['root_cause']}", f"**3. Cách làm:** {a['approach']}", "Đã loại: " + "; ".join(f"{x['option']} ({x['rejected_because']})" for x in a["alternatives"]),
               f"**4. Cách sửa chọn:** {a['fix']}", f"**5. Phạm vi ảnh hưởng:** {'; '.join(a['blast_radius']['affected']) or '(chỉ file mới)'} | rủi ro: {a['blast_radius']['risks']}",
               f"**6. Spec:** {a['spec']}"])


def cap_diff(diff, limit):
    """Cut a diff at a line boundary under `limit` bytes, saying so."""
    if len(diff.encode()) <= limit:
        return diff
    out, size = [], 0
    for line in diff.splitlines():
        size += len(line.encode()) + 1
        if size > limit:
            break
        out.append(line)
    return "\n".join(out) + f"\n... (diff cut at {limit} bytes: read the rest with Read/Grep)"


def scout_prompt(goal, spec, changed):
    return ("You are the scout of an automated engineering system. Read-only: do not edit anything. The merged result of a plan is in your current directory.\n"
            "Reviewers who decide whether to ship will rely on what you gather, so they do not have to read the repository again. Gather facts, do not judge.\n"
            f"Goal: {goal}\nSpec: {_cut(spec, 600)}\nChanged files: {', '.join(changed)}\n\n"
            "Read every changed file and its tests. Collect up to 40 facts: for each public function or branch that changed, where it is called from, which test "
            "exercises it (or that none does), how error paths and edge cases of the spec are handled, anything touching input validation, files, processes or secrets.\n"
            'Reply with ONLY one JSON object: {"facts":[{"file":"path","line":0,"quote":"one line copied exactly from that file","note":"what this line shows"}]}\n'
            "A fact whose quote is not found verbatim in the file is thrown away. To say something is missing, quote the line where it would be expected.")


def map_prompt(goal):
    return ("You are the scout of an automated engineering system. Read-only: do not edit anything. The repository is in your current directory.\n"
            "The planner who must plan this goal will rely on what you gather, so they do not have to search the repository. Gather facts, do not judge or design.\n"
            f"Goal: {goal}\n\n"
            "Find the files, functions and tests this goal touches, who calls them, what they import, how the code is laid out, the conventions to follow and how tests are run. Up to 40 facts.\n"
            'Reply with ONLY one JSON object: {"facts":[{"file":"path","line":0,"quote":"one line copied exactly from that file","note":"what this line shows"}]}\n'
            "A fact whose quote is not found verbatim in the file is thrown away.")


def parse_scout(obj, wt):
    """Keep a fact only when its quote is really a line of the file it names (inside the worktree); the line number is corrected to where the quote is."""
    root, facts, raw, cache = Path(wt).resolve(), [], [i for i in (obj.get("facts") if isinstance(obj.get("facts"), list) else []) if isinstance(i, dict)], {}
    for i in raw:
        f, quote = _s(i.get("file"), 300), _s(i.get("quote"), 200)
        path = (root / f).resolve()
        if not f or len(quote) < 3 or not path.is_relative_to(root) or not path.is_file():
            continue
        if f not in cache:
            cache[f] = path.read_text(errors="ignore").splitlines()
        hits = [n for n, l in enumerate(cache[f], 1) if quote in l]
        if hits:
            want = int(i["line"]) if str(i.get("line", "")).isdigit() else 0
            facts.append({"file": f, "line": min(hits, key=lambda n: abs(n - want)), "quote": quote, "note": _s(i.get("note"), 200)})
    return {"status": "ok", "facts": facts[:40], "dropped": len(raw) - len(facts)}


def scout_text(facts, limit=6000):
    """The block review and the SHIP personas get. Empty when there is no scout."""
    if not facts:
        return ""
    return ("Facts gathered by a scout that read the merged result (each quote was checked against the file by the harness; the notes are the scout's own reading, not checked). "
            "Decide from the diff and these facts. Open a file only when a fact you need is missing or you doubt a note.\n"
            + _cut("\n".join(f"- {x['file']}:{x['line']} `{x['quote']}`: {x['note']}" for x in facts), limit) + "\n\n")


def review_prompt(goal, spec, changed, diff, facts=""):
    return ("You are the review and simplification agent of an automated engineering system. Read-only: do not edit anything. The merged result of a plan is in your current directory.\n"
            f"Goal: {goal}\nSpec: {_cut(spec, 600)}\nChanged files: {', '.join(changed)}\nDiff:\n{diff}\n\n{facts}"
            'Reply with ONLY one JSON object: {"review":[{"file":"","line":0,"severity":"high|medium|low","problem":"","fix":""}],"simplify":[{"file":"","what":""}],'
            '"edge_cases":["edge cases you checked"]}\n'
            "review = wrong logic, unhandled edge cases, callers or behaviour broken, lost behaviour. simplify = dead or duplicated code, needless abstraction, things to delete. "
            "Report only what you can point to in the diff; use empty lists when it is clean.")


def parse_review(obj, changed):
    """Keep only findings about files that are in the diff (anything else is a guess); count the rest as dropped.
    An answer whose `review` is not a list of objects is not the review schema: not_run, never a silent empty 'ok'. An unknown severity counts as high (fail closed)."""
    items = obj.get("review") if isinstance(obj, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return {"status": "not_run", "error": "the answer is not the review schema"}

    def keep(items, fields):
        ok = [i for i in (items if isinstance(items, list) else []) if isinstance(i, dict)]
        good = [{f: _s(i.get(f), 250) for f in fields} for i in ok if _s(i.get("file"), 300) in changed][:10]
        return good, len(ok) - len([i for i in ok if _s(i.get("file"), 300) in changed])
    rev, d1 = keep(obj.get("review"), ("file", "line", "severity", "problem", "fix"))
    sim, d2 = keep(obj.get("simplify"), ("file", "what"))
    for r in rev:
        r["severity"] = r["severity"].lower() if r["severity"].lower() in ("high", "medium", "low") else "high"
    return {"status": "ok", "review": rev, "simplify": sim, "edge_cases": [_s(x, 150) for x in (obj.get("edge_cases") or []) if isinstance(x, str)][:8], "dropped": d1 + d2}


def _ship_row(sh):
    if sh.get("decision") not in ("GO", "NO-GO"):
        return "THIẾU", f"không chạy: {sh.get('decision') or 'không có'}"
    ps = sh.get("personas") or {}
    who = f"{sum(r['status'] == 'ok' for r in ps.values())}/{len(ps)} persona" if ps else ("bỏ fan-out (đổi nhỏ)" if sh.get("small") else "không gọi persona (plan chưa đạt)")
    return "OK", f"{sh['decision']}: {len(sh['blockers'])} chặn, {len(sh['recommended'])} nên sửa, có kế hoạch rollback; {who}"


def _scan_row(r, gate_name, last, done, what, limit=""):
    """Security and code-verify rows: ran or not, what it found, how many steps passed the same scan as a gate."""
    if not r or r.get("status") != "ok":
        return "THIẾU", f"không chạy: {(r or {}).get('error') or 'không có'}"
    ok = sum(f"{gate_name}:ok" in g for g in last)
    return ("MỘT PHẦN" if limit else "OK"), f"{what}: {r['n_hard']} chặn, {r['n_soft']} nên xem; gate qua ở {ok}/{len(done)} bước" + (f"; {limit}" if limit else "")


def step_brief(plan):
    """What each step agent is told: the user's confirmed success criteria and non-goals, then the analysis."""
    b = plan.get("brief")
    req = (f"Requirements the user confirmed: success: {_cut(b['success'], 200)}; must not change: {_cut('; '.join(b['impact']), 200)}; non-goals: {_cut('; '.join(b['non_goals']) or '-', 200)}; "
           f"edge cases: {_cut('; '.join(b['risks']) or '-', 200)}\n" if b else "")
    return req + brief(plan.get("analysis"))


def coverage(plan, st):
    """(number, name, status, evidence) for each of the 16 steps, from facts in plan.json / state.json. status: OK, MỘT PHẦN or THIẾU."""
    a, b, steps = plan.get("analysis"), plan.get("brief"), st["steps"]
    done = [e for e in steps.values() if e["status"] == "done"]
    last = [e["attempts"][-1].get("gates", "") for e in done if e.get("attempts")]
    blast = sum("blast_radius:ok" in g for g in last)
    integ, rv = st.get("integration"), st.get("review") or {}
    told = "LLM tự viết, harness chỉ kiểm có mặt"
    no_a = ("THIẾU", "plan không có khối analysis")

    def has(k):
        return ("MỘT PHẦN", f"có, {told}") if a and a.get(k) else no_a
    rows = [("Interview (làm rõ yêu cầu)", *((("OK" if b["confidence"] >= 95 and all(v == "known" for v in b["coverage"].values()) else "MỘT PHẦN"),
                                                f"{len(b['qa'])} câu hỏi, {sum(v == 'known' for v in b['coverage'].values())}/{len(b['coverage'])} mục (ai, vì sao, ảnh hưởng, thành công, ràng buộc, ngoài phạm vi, rủi ro) đã rõ, độ tự tin {b['confidence']}% (agent tự báo), user xác nhận brief"
                                                + (f"; còn {len(b['assumptions'])} giả định chưa xác nhận" if b["assumptions"] else "")) if b else ("THIẾU", "không phỏng vấn: yêu cầu đi thẳng vào plan"))),
            ("Research", *(("OK", f"{len(a['research'])} file dẫn chứng, đều tồn tại ở base") if a else no_a)), ("Thinking Why (nguyên nhân gốc)", *has("root_cause")),
            ("How can do that (cách làm)", *(("MỘT PHẦN", f"có + {len(a['alternatives'])} phương án bị loại, {told}") if a else no_a)), ("How can fix that (cách sửa)", *has("fix")),
            ("Did it effect anything (blast radius)", *(("OK", f"gate blast_radius qua ở {blast}/{len(done)} bước; cảnh báo: drift {sum(bool(e.get('drift')) for e in steps.values())}, "
                                                              f"trùng file {len(st.get('overlap') or [])}") if a and blast == len(done) and done else ("MỘT PHẦN", f"gate blast_radius qua {blast}/{len(done)} bước" + ("" if a else "; không có phân tích")))),
            ("Spec", *has("spec")), ("Plan", "OK", f"{len(steps)} bước, khoá bằng hash `{st.get('plan_hash', '')}` khi approve"),
            ("Build", *(("OK", f"{len(done)}/{len(steps)} bước đã merge") if len(done) == len(steps) else ("MỘT PHẦN", f"{len(done)}/{len(steps)} bước đã merge"))),
            ("Test", *(("OK", "gates + acceptance từng bước + chạy lại trên branch đã merge") if integ and integ["ok"] else ("MỘT PHẦN" if integ else "THIẾU", "kiểm tra tích hợp không đạt" if integ else "không chạy tích hợp"))),
            ("Security hardening", *_scan_row(st.get("security"), "security", last, done, "quét mẫu code nguy hiểm (eval, shell, SQL ghép chuỗi, pickle, TLS, XSS, cookie, chmod)",
                                              "auth, rate limit, SSRF, quyền sở hữu tài nguyên KHÔNG kiểm tự động; security-auditor ở Ship đọc thêm")),
            ("Review", *(("OK", f"{len(rv['review'])} phát hiện ({sum(r['severity'] == 'high' for r in rv['review'])} high), chỉ tính file có trong diff") if rv.get("status") == "ok"
                         else ("THIẾU", f"không chạy: {rv.get('error') or rv.get('status') or 'không có'}"))),
            ("Simply Code", *(("OK", f"{len(rv['simplify'])} đề xuất tinh gọn (không tự sửa)") if rv.get("status") == "ok" else ("THIẾU", "không chạy cùng lượt review"))),
            ("Code verify (chống code ẩu)", *_scan_row(st.get("verify"), "code_verify", last, done, "stub, debugger, except nuốt lỗi, test không assert, file khai báo rỗng, import thừa")),
            ("Report", "OK", "file này"), ("Ship (GO/NO-GO)", *_ship_row(st.get("ship") or {}))]
    return [(i, *r) for i, r in enumerate(rows, 1)]

# --- 13 SHIP: the /ship command of agent-skills, as code -----------------------------------------------------------------
PERSONAS = {
    "code-reviewer": "Five-axis review of the change: correctness, readability, architecture, security, performance. Severity: critical (breaks behaviour or data), important (should fix before ship), suggestion.",
    "security-auditor": "Vulnerability and threat-model pass: OWASP Top 10, input validation, secrets handling, auth/authz, injection, unsafe file or process use, dependency CVEs when a manifest changed. Severity: critical, high, medium, low.",
    "test-engineer": "Coverage analysis of the change: which happy paths, edge cases, error paths and concurrency cases have no test (Read the existing tests). `problem` = the gap, `fix` = the test to add. Severity: critical (core path untested), important, suggestion.",
}
SENSITIVE_PATH = re.compile(r"(?i)(auth|login|passw|session|token|secret|credential|payment|billing|checkout|invoice|migrat|schema|\.sql$|database|(^|/)db[/._]|models?[/.]|\.env|config|setting|docker|compose|\.ya?ml$|\.toml$|\.github/)")
SENSITIVE_TEXT = re.compile(r"(?i)\b(password|passwd|secret|token|credential|login_required|permission|authori[sz]|authenticat|csrf|select\s.+\sfrom|insert\s+into|delete\s+from|\.execute\()")
INFRA_PATH = re.compile(r"(?i)(\.env|config|setting|docker|compose|migrat|schema|\.sql$|\.ya?ml$|\.toml$|\.github/|requirements|package\.json|pyproject)")
UI_PATH = re.compile(r"(?i)\.(html?|css|scss|jsx|tsx|vue|svelte)$")
DOC_PATH = re.compile(r"(?i)(\.(md|rst)$|(^|/)docs?/|changelog)")
SEV = ("critical", "high", "important", "medium", "low", "suggestion")


def is_small(changed, lines, diff):
    """ship rule 5: the fan-out may be skipped only for <=2 files, <50 changed lines and nothing touching auth, payments, data access or config."""
    return len(changed) <= 2 and lines < 50 and not any(SENSITIVE_PATH.search(f) for f in changed) and not SENSITIVE_TEXT.search(
        "\n".join(l for l in diff.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---"))))  # removed lines too: dropping a guard is a change to it


def persona_prompt(name, goal, spec, changed, diff, facts=""):
    return (f"You are the {name} persona of an automated engineering system's ship check. Read-only: do not edit anything, do not call other agents. The merged result is in your current directory.\n"
            f"{PERSONAS[name]}\nGoal: {goal}\nSpec: {_cut(spec, 600)}\nChanged files: {', '.join(changed)}\nDiff:\n{diff}\n\n{facts}"
            'Reply with ONLY one JSON object: {"findings":[{"file":"","line":0,"severity":"","problem":"","fix":""}]'
            + (',"rollback_triggers":["observable signals that should make the team roll this change back"]' if name == "code-reviewer" else "")
            + '}. Report only what you can point to in the diff or in files it touches; an empty list when clean.')


def parse_persona(name, obj, changed):
    """Findings about files in the diff only (counted as dropped otherwise). An answer that is not the findings schema is not_run, never a silent GO;
    an unknown severity counts as critical (fail closed); the 12 kept are the most severe."""
    items = obj.get("findings") if isinstance(obj, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return {"status": "not_run", "error": "the answer is not the findings schema"}
    mine = [i for i in items if _s(i.get("file"), 300).removeprefix("./") in changed]
    out = []
    for i in mine:
        sev = _s(i.get("severity"), 20).lower()
        out.append({"file": _s(i.get("file"), 300).removeprefix("./"), "line": _s(i.get("line"), 10), "severity": sev if sev in SEV else "critical", "problem": _s(i.get("problem"), 250), "fix": _s(i.get("fix"), 250)})
    out.sort(key=lambda f: SEV.index(f["severity"]))
    trig = [_s(x, 150) for x in (obj.get("rollback_triggers") or []) if isinstance(x, str)][:5] if name == "code-reviewer" else []
    return {"status": "ok", "findings": out[:12], "dropped": len(items) - len(mine), "rollback_triggers": trig}


def rollback(pid, base, head, files, triggers):
    """Mandatory before any GO: when to roll back, the exact commands, and an honest recovery estimate."""
    risky = [f for f in files if INFRA_PATH.search(f)]
    return {"triggers": (triggers or []) + ["test hoặc acceptance của plan fail sau khi merge", "lỗi mới trong log liên quan tới file đã đổi (Loki {job=\"flow\"})"],
            "procedure": [f"chưa merge: `git branch -D flow/{pid}` (nhánh của bạn chưa bị đụng)", f"đã merge bằng merge commit: `git revert -m 1 <merge-commit>`",
                          f"đã fast-forward: `git revert --no-edit {base[:10]}..{head or 'flow/' + pid}`"],
            "rto": "vài phút: chỉ cần lệnh git" if not risky else f"git không đủ: {', '.join(risky[:4])} là cấu hình, hạ tầng hoặc dữ liệu, phải hoàn tác tay thêm"}


def decide(plan, st, personas, files, small, step_review):
    """The merge phase, in code: blockers, recommended fixes, acknowledged risks. Any blocker (critical finding, failed integration, unfinished steps,
    a persona that did not run) means NO-GO; GO always comes with a rollback plan."""
    blockers, rec, risks = [], [], []
    steps = st["steps"]
    if any(e["status"] != "done" for e in steps.values()):
        blockers.append("harness: chưa đủ bước done (" + ", ".join(f"{k}={e['status']}" for k, e in steps.items() if e["status"] != "done") + ")")
    if not files:
        blockers.append("harness: diff gộp rỗng, không có thay đổi nào để ship")
    integ = st.get("integration")
    if not integ or not integ["ok"]:
        blockers.append("harness: kiểm tra tích hợp không đạt" if integ else "harness: không có kiểm tra tích hợp")
    for name, r in personas.items():
        if r["status"] != "ok":
            blockers.append(f"{name}: không chạy ({r.get('error') or r['status']}), thiếu báo cáo nên không thể GO")
            continue
        for f in r["findings"]:
            line = f"{name}: [{f['severity']}] `{f['file']}:{f['line']}` {f['problem']} → {f['fix']}"
            if f["severity"] == "critical" or (name == "security-auditor" and f["severity"] == "high"):
                blockers.append(line)
            elif f["severity"] in ("high", "important", "medium"):
                rec.append(line)
    for key, label in (("security", "hardening"), ("verify", "code-verify")):
        r = st.get(key)
        if not r or r.get("status") != "ok":
            blockers.append(f"{label}: không chạy ({(r or {}).get('error') or 'không có'}), thiếu bằng chứng nên không thể GO")
            continue
        blockers += [f"{label}: {h}" for h in r["hard"][:6]] + ([f"{label}: còn {r['n_hard'] - 6} chặn nữa"] if r["n_hard"] > 6 else [])
        rec += [f"{label}: {h}" for h in r["soft"][:4]] + ([f"{label}: còn {r['n_soft'] - 4} mục nên xem nữa"] if r["n_soft"] > 4 else [])
    if (step_review or {}).get("status") == "not_run":  # advisory, but a review that did not run must not read as a clean one
        rec.append(f"review: không chạy ({step_review.get('error') or 'không rõ lý do'})")
    for r in (step_review or {}).get("review", []):
        if r["severity"] == "high":  # parse_review kept only files of the diff: a high finding that points at a line is a blocker
            (blockers if str(r.get("line", "")).isdigit() and int(r["line"]) > 0 else rec).append(f"review: `{r['file']}:{r['line']}` {r['problem']}")
    drift = sorted({p for e in steps.values() for p in e.get("drift", [])})
    if drift:
        rec.append(f"file ngoài kế hoạch được merge: {', '.join(drift[:6])}")
    if not small and any(not DOC_PATH.search(f) and not is_test_path(f) for f in files) and not any(DOC_PATH.search(f) for f in files):
        rec.append("tài liệu (README, CHANGELOG, docs) không đổi dù code đổi")
    infra = [f for f in files if INFRA_PATH.search(f)]
    if infra:
        risks.append(f"đổi cấu hình, hạ tầng hoặc dữ liệu, engine không kiểm được ngoài test: {', '.join(infra[:5])}")
    ui = [f for f in files if UI_PATH.search(f)]
    if ui:
        risks.append(f"đổi giao diện ({', '.join(ui[:4])}): bàn phím, screen reader, contrast chưa được kiểm, cần kiểm tay")
    if small:
        risks.append("đổi nhỏ (<=2 file, <50 dòng, không đụng auth/thanh toán/dữ liệu/cấu hình): bỏ fan-out theo luật của /ship, chỉ dựa vào bằng chứng của harness")
    return {"decision": "NO-GO" if blockers else "GO", "blockers": blockers, "recommended": rec, "risks": risks}


def is_test_path(f):
    return bool(re.search(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.[a-z]+$)", f))


def ship_md(sh):
    """Phase C of /ship, for report.md."""
    if sh.get("decision") in (None, "off"):
        return []
    lines = [f"## Quyết định SHIP: {sh['decision']}", "", "### Chặn (phải sửa trước khi ship)"] + ([f"- {b}" for b in sh["blockers"]] or ["- (không có)"])
    lines += ["", "### Nên sửa trước khi ship"] + ([f"- {r}" for r in sh["recommended"]] or ["- (không có)"])
    lines += ["", "### Rủi ro đã biết (vẫn ship)"] + ([f"- {r}" for r in sh["risks"]] or ["- (không có)"])
    rb = sh["rollback"]
    lines += ["", "### Kế hoạch rollback", "- khi nào rollback: " + "; ".join(rb["triggers"]), *[f"- cách: {p}" for p in rb["procedure"]], f"- thời gian khôi phục: {rb['rto']}", "",
              "### Ba persona (chạy song song)"]
    lines += [f"- {n}: {r['status']}" + (f", {len(r['findings'])} phát hiện, bỏ {r['dropped']} ngoài diff" if r["status"] == "ok" else f" ({r.get('error', '')})") for n, r in sh["personas"].items()] or ["- không gọi persona"]
    return lines


def audit_md(title, r):
    """A security-hardening or code-verify section for report.md ([] when the scan did not run)."""
    if not r or r.get("status") != "ok":
        return []
    return ["", f"## {title}", *([f"- CHẶN {h}" for h in r["hard"]] or ["- không có mục chặn"]), *[f"- nên xem {x}" for x in r["soft"]],
            *([f"- (còn {r['n_hard'] + r['n_soft'] - len(r['hard']) - len(r['soft'])} mục nữa, xem state.json)"] if r["n_hard"] + r["n_soft"] > len(r["hard"]) + len(r["soft"]) else [])]
