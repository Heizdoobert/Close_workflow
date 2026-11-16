"""Checks on what the planner and the agents claim (stdlib only).

write_analysis saves the planner's analysis as analysis.md before any step runs; check_research flags research files that
are not real files in the worktree; machine_check reruns each finished step's acceptance command and compares the result
with the PASS/FAIL the agent claimed. The harness cannot verify that a root cause or spec is true, only these facts.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import tempfile
from typing import Any

from flow_engine.secrets import redact


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _analysis_lines(analysis: dict) -> list[str]:
    lines = ["# Phân tích (analysis)", "", "## Research"]
    for item in analysis.get("research") or []:
        if isinstance(item, dict):
            lines.append(f"- `{_s(item.get('file'))}`: {_s(item.get('finding'))}")
    lines += ["", "## Root cause", _s(analysis.get("root_cause")), "", "## Approach", _s(analysis.get("approach")), "", "## Alternatives"]
    for item in analysis.get("alternatives") or []:
        if isinstance(item, dict):
            lines.append(f"- {_s(item.get('option'))}: {_s(item.get('rejected_because'))}")
    blast = analysis.get("blast_radius") if isinstance(analysis.get("blast_radius"), dict) else {}
    lines += ["", "## Fix", _s(analysis.get("fix")), "", "## Blast radius"]
    for path in blast.get("affected") or []:
        lines.append(f"- affected: {_s(path)}")
    lines += [f"Risks: {_s(blast.get('risks'))}", "", "## Spec", _s(analysis.get("spec")), ""]
    return lines


def analysis_text(analysis) -> str:
    """The exact text write_analysis stores: the analysis block as Markdown, redacted."""
    return redact("\n".join(_analysis_lines(analysis)))


def write_analysis(d, analysis):
    """Write d/analysis.md (atomically, redacted) from the plan's analysis block. Returns the path, or None when there is no analysis."""
    if not analysis or not isinstance(analysis, dict):
        return None
    d = pathlib.Path(d)
    target = d / "analysis.md"
    text = analysis_text(analysis)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".analysis-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def file_sha(path):
    """SHA-256 of the file's text (utf-8), or None when the file does not exist."""
    if not os.path.exists(path):
        return None
    text = pathlib.Path(path).read_text(encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def expected_sha(analysis):
    """SHA-256 of analysis_text(analysis), or None when there is no analysis."""
    if not analysis or not isinstance(analysis, dict):
        return None
    return hashlib.sha256(analysis_text(analysis).encode("utf-8")).hexdigest()


def check_analysis(d, state, analysis) -> list:
    """Blocker when d/analysis.md no longer matches the hash taken when the plan was made (state's analysis_sha first)."""
    expected = (state or {}).get("analysis_sha") or expected_sha(analysis)
    if not expected:
        return []
    actual = file_sha(pathlib.Path(d) / "analysis.md")
    if actual != expected:
        return ["claims: analysis.md không khớp hash lúc lập plan (có thể đã bị sửa)"]
    return []


def inside_root(root, rel):
    """The real file at rel under root, or None when rel is not a relative path to a file that stays inside root (symlinks resolved)."""
    rel = "" if rel is None else str(rel)
    if not rel or "\\" in rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None
    if ".." in pathlib.PurePosixPath(rel).parts:
        return None
    candidate = pathlib.Path(root) / rel
    if not candidate.is_file():
        return None
    real = os.path.realpath(candidate)
    base = os.path.realpath(root)
    try:
        if os.path.commonpath([real, base]) != base:
            return None
    except ValueError:
        return None
    return pathlib.Path(real)


def check_research(analysis, wt) -> list[str]:
    """Problems with the research files: each must be a relative path to an existing file inside the worktree."""
    problems = []
    for item in (analysis or {}).get("research") or []:
        file = _s(item.get("file", "")) if isinstance(item, dict) else ""
        if inside_root(wt, file) is None:
            problems.append(f"research không căn cứ: {file}")
    return problems


def machine_check(plan, state, accept, wt, claimed) -> dict:
    """Rerun the acceptance of every finished step and compare it with the claimed PASS/FAIL (None = no claim, no comparison)."""
    rows, blockers = [], []
    steps_state = (state or {}).get("steps") or {}
    for step in plan.get("steps") or []:
        sid = step.get("id")
        if (steps_state.get(sid) or {}).get("status") != "done" or not step.get("acceptance"):
            continue
        actual = bool(accept(step, wt).get("ok"))
        c = (claimed or {}).get(sid) if claimed else None
        if c not in ("PASS", "FAIL"):
            c = None
        match = None if c is None else ((c == "PASS") == actual)
        rows.append({"step": sid, "claimed": c, "actual": actual, "match": match})
        if match is False:
            blockers.append(f"claims: bước {sid} khai {c} nhưng acceptance chạy lại cho {'PASS' if actual else 'FAIL'}")
    return {"ok": not blockers, "rows": rows, "blockers": blockers}
