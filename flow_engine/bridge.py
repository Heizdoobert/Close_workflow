"""Link to the Agent_system package (`agent-system` CLI): recall before a task, learn after it. Always optional."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def agent_system():
    """Find agent-system next to this interpreter (shared venv) or on PATH."""
    exe = "agent-system.exe" if os.name == "nt" else "agent-system"
    for cand in (Path(sys.executable).parent / exe, shutil.which("agent-system")):
        if cand and Path(cand).is_file():
            return str(cand)
    return None


def run_agent(*args, cwd=None, timeout=60):
    exe = agent_system()
    if not exe:
        return subprocess.CompletedProcess(args, 127, "", "agent-system not installed")
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def recall(message, max_bytes=1200):
    """Prior insights relevant to the task, as a short prompt prefix ('' when nothing useful)."""
    if os.environ.get("FLOW_RECALL", "1") != "1":
        return ""
    p = run_agent("memory", "search", "--query", message[:300], "--top-k", "3", timeout=30)
    try:
        items = [r["content"][:200] for r in json.loads(p.stdout)]
    except Exception:
        return ""
    text = "Known from earlier tasks (use only if relevant):\n" + "\n".join(f"- {i}" for i in items)
    return text.encode()[:max_bytes].decode(errors="ignore") + "\n\n" if items else ""


def learn(task_id, message, result):
    """Index a finished task so the next recall can find it."""
    if os.environ.get("FLOW_LEARN", "1") != "1" or not result:
        return
    run_agent("memory", "index", "--id", f"flow-{task_id}", "--category", "core_knowledge",
              "--content", f"task: {message[:150]} -> {result[:300]}", timeout=60)


def record_recipe(names, text):
    run_agent("memory", "recipe-record", "--key", f"flow-gate:{names}", "--recipe", text, timeout=60)
