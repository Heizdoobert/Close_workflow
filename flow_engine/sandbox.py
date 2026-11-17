"""OS sandbox for claude sub-agents: the step worktree is the only project path they can write.

Linux with bubblewrap (bwrap): the whole filesystem is mounted read-only, and only these paths are writable:
the step worktree, /tmp, and claude's own config (~/.claude, ~/.claude.json). Network stays on because the API needs it.
Sub-agents get no environment variable whose name matches secrets.SECRET_ENV (see secrets.agent_env). bwrap also hides the
secret directories and files listed in SECRET_DIRS and SECRET_FILES below, and the engine .env. Still visible: claude's own
credentials under ~/.claude and ~/.claude.json, and the network.
Commit and tag signing is off inside the sandbox because the signing keys are hidden, and agents must not sign as the user.
Without bwrap nothing stops a write; plan.py's escaping_paths post-check after each step is the only guard.
agy is not wrapped: its own sandbox already mounts only /mnt/second_drive and /tmp. opencode is not wrapped either.
So the secret paths are hidden from claude only: agy, opencode and any run without bwrap can still read them,
and a secret path inside the bound worktree is not hidden.
"""

import functools
import hashlib
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


SECRET_DIRS = ['.ssh', '.gnupg', '.aws', '.kube', '.gemini', '.config/gh', '.config/gcloud']
SECRET_FILES = ['.netrc', '.git-credentials', '.pypirc', '.npmrc', '.docker/config.json', '.local/share/opencode/auth.json']


def _inside(p, wd_path):
    return p.resolve() == wd_path or wd_path in p.resolve().parents


def _hidden(wd):
    """bwrap flags that hide the secret dirs and files that exist (an empty tmpfs or /dev/null over them); never the worktree.
    bwrap binds are nodev, so a hidden file is not empty but unreadable: opening it fails with "Permission denied"."""
    from flow_engine import engine  # lazy: engine imports agents, which imports this module
    home = Path.home()
    wd_path = Path(wd).resolve()

    out = []
    for rel in SECRET_DIRS:
        p = home / rel
        if p.is_dir() and not _inside(p, wd_path):
            out += ["--tmpfs", str(p)]
    for rel in SECRET_FILES:
        p = home / rel
        if p.is_file() and not _inside(p, wd_path):
            out += ["--ro-bind", "/dev/null", str(p)]
    env_file = Path(engine.ROOT) / ".env"
    if env_file.is_file() and not _inside(env_file, wd_path):
        out += ["--ro-bind", "/dev/null", str(env_file)]
    return out


def _no_signing(wd):
    """bwrap flags that bind a copy of the global git config with commit and tag signing off over the real one.
    The signing keys are hidden in the sandbox, so a signed commit would fail (rc 128)."""
    from flow_engine import engine  # lazy: engine imports agents, which imports this module
    home = Path.home()
    src = next((p for p in (home / ".gitconfig", home / ".config" / "git" / "config") if p.is_file()), None)
    if src is None:
        return []
    try:
        if _inside(src, Path(wd).resolve()):
            return []
        text = src.read_text() + "\n[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
        # the name carries a hash of the text, so a fake HOME in a test never overwrites the copy a live agent binds
        copy = Path(engine.ROOT) / "logs" / ("sandbox-gitconfig-" + hashlib.sha256(text.encode()).hexdigest()[:12])
        copy.parent.mkdir(parents=True, exist_ok=True)
        tmp = copy.parent / (".tmp-" + str(os.getpid()))
        tmp.write_text(text)
        tmp.chmod(0o600)
        os.replace(tmp, copy)
    except OSError:
        return []
    return ["--ro-bind", str(copy), str(src)]


def wrap(argv, workdir):
    """argv prefixed with bwrap: read-only /, read-write /tmp, the worktree and claude's config, secret paths hidden. Callers check available() first."""
    wd = str(Path(workdir).resolve())
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--bind", "/tmp", "/tmp", "--bind", wd, wd]
    for p in _claude_homes():
        cmd += ["--bind", str(p), str(p)]
    cmd += _hidden(wd)
    cmd += _no_signing(wd)
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
