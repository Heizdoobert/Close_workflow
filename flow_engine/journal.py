"""Per-step journal: one JSON line per attempt, so a retried, resumed or looping step knows what it already tried.

Layout: <plan dir>/journal/<sid>.jsonl. Every string is passed through plan.redact() before it is written.
"""

import json


def _plan():
    from flow_engine import plan  # imported here: plan imports this module at load time
    return plan


def _path(pid, sid):
    return _plan().plans_dir() / pid / "journal" / f"{sid}.jsonl"


def _clean(v):
    if isinstance(v, str):
        return _plan().redact(v)
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def append(pid, sid, entry):
    p = _path(pid, sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(_clean(entry), ensure_ascii=False) + "\n")


def read(pid, sid):
    p = _path(pid, sid)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:  # a crash can leave one half-written line at the end
            continue
    return out


def _line(e):
    parts = [f"attempt {e.get('n', '?')} {e.get('status', '?')}"]
    if e.get("backend"):
        parts.append(f"via {e['backend']}")
    if e.get("failed"):
        parts.append("failed: " + ", ".join(e["failed"]))
    if e.get("files"):
        parts.append("files: " + ", ".join(e["files"][:10]))
    if e.get("drift"):
        parts.append("drift: " + ", ".join(e["drift"][:10]))
    if e.get("error"):
        parts.append("error: " + e["error"][:120])
    if e.get("reflection"):
        parts.append(e["reflection"])
    return " | ".join(parts)


def brief(pid, sid, limit=1200):
    """Compact text of the earlier attempts, newest whole lines first, at most `limit` UTF-8 bytes."""
    lines = [_line(e) for e in read(pid, sid)]
    kept, used = [], 0
    for line in reversed(lines):
        size = len(line.encode("utf-8")) + 1
        if used + size > limit:
            break
        kept.append(line)
        used += size
    if not kept and lines:  # one line alone is over the limit: cut it on a character boundary
        kept = [lines[-1].encode("utf-8")[:limit].decode("utf-8", errors="ignore")]
    return "\n".join(reversed(kept))
