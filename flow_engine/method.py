"""The 12-step working method, as far as code can hold it.

 1 research  2 why (root cause)  3 how (approach + rejected alternatives)  4 fix (the chosen one)  5 blast radius  6 spec   -> `analysis` block of plan.json
 7 plan  8 build  9 test                                                                                                    -> plan.py steps, gate.py, acceptance
 10 review  11 simplify                                                                                                     -> one read-only agent call over the merged diff
 12 report                                                                                                                  -> report.md, with a coverage table

What the harness checks itself: research cites files that exist at the base commit, every analysis field is present, a step may not delete, rename
or orphan anything it did not declare (gate.blast_radius), review findings must point at files that are in the diff. What it cannot check: that the
root cause or the spec is true. The coverage table says which is which.
"""

import re

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


def review_prompt(goal, spec, changed, diff):
    return ("You are the review and simplification agent of an automated engineering system. Read-only: do not edit anything. The merged result of a plan is in your current directory.\n"
            f"Goal: {goal}\nSpec: {_cut(spec, 600)}\nChanged files: {', '.join(changed)}\nDiff:\n{diff}\n\n"
            'Reply with ONLY one JSON object: {"review":[{"file":"","line":0,"severity":"high|medium|low","problem":"","fix":""}],"simplify":[{"file":"","what":""}],'
            '"edge_cases":["edge cases you checked"]}\n'
            "review = wrong logic, unhandled edge cases, callers or behaviour broken, lost behaviour. simplify = dead or duplicated code, needless abstraction, things to delete. "
            "Report only what you can point to in the diff; use empty lists when it is clean.")


def parse_review(obj, changed):
    """Keep only findings about files that are in the diff (anything else is a guess); count the rest as dropped."""
    def keep(items, fields):
        ok = [i for i in (items if isinstance(items, list) else []) if isinstance(i, dict)]
        good = [{f: _s(i.get(f), 250) for f in fields} for i in ok if _s(i.get("file"), 300) in changed][:10]
        return good, len(ok) - len([i for i in ok if _s(i.get("file"), 300) in changed])
    rev, d1 = keep(obj.get("review"), ("file", "line", "severity", "problem", "fix"))
    sim, d2 = keep(obj.get("simplify"), ("file", "what"))
    for r in rev:
        r["severity"] = r["severity"].lower() if r["severity"].lower() in ("high", "medium", "low") else "medium"
    return {"status": "ok", "review": rev, "simplify": sim, "edge_cases": [_s(x, 150) for x in (obj.get("edge_cases") or []) if isinstance(x, str)][:8], "dropped": d1 + d2}


def coverage(plan, st):
    """(number, name, status, evidence) for each of the 12 steps, from facts in plan.json / state.json. status: OK, MỘT PHẦN or THIẾU."""
    a, steps = plan.get("analysis"), st["steps"]
    done = [e for e in steps.values() if e["status"] == "done"]
    last = [e["attempts"][-1].get("gates", "") for e in done if e.get("attempts")]
    blast = sum("blast_radius:ok" in g for g in last)
    integ, rv = st.get("integration"), st.get("review") or {}
    told = "LLM tự viết, harness chỉ kiểm có mặt"
    no_a = ("THIẾU", "plan không có khối analysis")

    def has(k):
        return ("MỘT PHẦN", f"có, {told}") if a and a.get(k) else no_a
    rows = [("Research", *(("OK", f"{len(a['research'])} file dẫn chứng, đều tồn tại ở base") if a else no_a)), ("Thinking Why (nguyên nhân gốc)", *has("root_cause")),
            ("How can do that (cách làm)", *(("MỘT PHẦN", f"có + {len(a['alternatives'])} phương án bị loại, {told}") if a else no_a)), ("How can fix that (cách sửa)", *has("fix")),
            ("Did it effect anything (blast radius)", *(("OK", f"gate blast_radius qua ở {blast}/{len(done)} bước; cảnh báo: drift {sum(bool(e.get('drift')) for e in steps.values())}, "
                                                              f"trùng file {len(st.get('overlap') or [])}") if a and blast == len(done) and done else ("MỘT PHẦN", f"gate blast_radius qua {blast}/{len(done)} bước" + ("" if a else "; không có phân tích")))),
            ("Spec", *has("spec")), ("Plan", "OK", f"{len(steps)} bước, khoá bằng hash `{st.get('plan_hash', '')}` khi approve"),
            ("Build", *(("OK", f"{len(done)}/{len(steps)} bước đã merge") if len(done) == len(steps) else ("MỘT PHẦN", f"{len(done)}/{len(steps)} bước đã merge"))),
            ("Test", *(("OK", "gates + acceptance từng bước + chạy lại trên branch đã merge") if integ and integ["ok"] else ("MỘT PHẦN" if integ else "THIẾU", "kiểm tra tích hợp không đạt" if integ else "không chạy tích hợp"))),
            ("Review", *(("OK", f"{len(rv['review'])} phát hiện ({sum(r['severity'] == 'high' for r in rv['review'])} high), chỉ tính file có trong diff") if rv.get("status") == "ok"
                         else ("THIẾU", f"không chạy: {rv.get('error') or rv.get('status') or 'không có'}"))),
            ("Simply Code", *(("OK", f"{len(rv['simplify'])} đề xuất tinh gọn (không tự sửa)") if rv.get("status") == "ok" else ("THIẾU", "không chạy cùng lượt review"))),
            ("Report", "OK", "file này")]
    return [(i, *r) for i, r in enumerate(rows, 1)]
