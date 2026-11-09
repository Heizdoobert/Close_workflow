"""OS sandbox for claude sub-agents: the step worktree is the only project path they can write.

Linux with bubblewrap (bwrap): the whole filesystem is mounted read-only, and only these paths are writable:
the step worktree, /tmp, and claude's own config (~/.claude, ~/.claude.json). Network stays on because the API needs it.
Without bwrap nothing stops a write; plan.py's escaping_paths post-check after each step is the only guard.
agy is not wrapped: its own sandbox already mounts only /mnt/second_drive and /tmp.
"""

import functools
import json
import os
import shutil
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=None)
def available():
    """'bwrap' when bubblewrap can really create a sandbox here (on PATH is not enough: user namespaces may be blocked), else None."""
    if os.name == "nt" or not shutil.which("bwrap"):
        return None
    try:
        p = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "bwrap" if p.returncode == 0 else None


def _claude_homes():
    home = Path.home()
    return [p for p in (home / ".claude", home / ".claude.json") if p.exists()]


def wrap(argv, workdir):
    """argv prefixed with bwrap: read-only /, read-write /tmp, the worktree and claude's config. Callers check available() first."""
    wd = str(Path(workdir).resolve())
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--bind", "/tmp", "/tmp", "--bind", wd, wd]
    for p in _claude_homes():
        cmd += ["--bind", str(p), str(p)]
    return cmd + ["--die-with-parent", "--"] + list(argv)


_warned = False


def warn_no_sandbox():
    """One warning per process: without a sandbox the only write check is escaping_paths, after the step has run."""
    global _warned
    if _warned:
        return
    _warned = True
    from flow_engine import engine  # lazy: engine imports agents, which imports this module
    engine.log(json.dumps({"ts": engine.now(), "event": "sandbox", "level": "warning", "sandbox": None,
                           "message": "no bwrap: sub-agent writes are only checked after each step by escaping_paths in plan.py"}))
