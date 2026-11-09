#!/usr/bin/env python3
"""One installer for Linux, macOS and Windows (stdlib only).

  python install.py                       venv + flow-task + agent-system + Redis (Docker) + MCP registration
  python install.py --service             also start the listener at login (systemd --user / launchd / Task Scheduler)
  python install.py --no-docker           skip containers (point REDIS_HOST/.env at your own Redis)
  python install.py --agent-system PATH   Agent_system source dir or pip URL (default: ../Agent_system, else cloned into vendor/)
"""
import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = os.name == "nt"
VENV = ROOT / ".venv"
BIN = VENV / ("Scripts" if WIN else "bin")


def say(msg):
    print(f"[install] {msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], **kw)


def py(*args):
    return run([BIN / ("python.exe" if WIN else "python"), *args])


AGENT_URL = os.environ.get("AGENT_SYSTEM_URL", "https://github.com/Heizdoobert/Agent_update_smart_working.git")


def find_agent_src(explicit):
    """Agent_system source: --agent-system, $AGENT_SYSTEM_SRC, sibling folder, vendor/ (cloned earlier), else clone it now."""
    for cand in (explicit, os.environ.get("AGENT_SYSTEM_SRC"), ROOT.parent / "Agent_system", ROOT / "vendor" / "Agent_system"):
        if cand and (Path(cand) / "pyproject.toml").exists():
            return Path(cand)
        if cand and "://" in str(cand):
            return str(cand)
    if not shutil.which("git"):
        say("git not found: cannot fetch Agent_system (install git, or pass --agent-system PATH)")
        return None
    dest = ROOT / "vendor" / "Agent_system"
    say(f"cloning Agent_system from {AGENT_URL}")
    dest.parent.mkdir(exist_ok=True)
    if run(["git", "clone", "-q", "--depth", "1", AGENT_URL, dest]).returncode != 0:
        say("clone failed (offline or private repo?): memory and MCP tools stay off; re-run with --agent-system PATH_OR_URL")
        return None
    return dest


def make_venv():
    if not (BIN / ("python.exe" if WIN else "python")).exists():
        say(f"creating {VENV}")
        run([sys.executable, "-m", "venv", VENV], check=True)


def install_packages(agent_src, embed):
    py("-m", "pip", "install", "-q", "-U", "pip")  # best effort
    py("-m", "pip", "install", "-q", "-e", ROOT).check_returncode()
    if not agent_src:
        say("agent-system source not found: memory recall/learn and MCP tools stay off (use --agent-system PATH)")
        return
    spec = str(agent_src)
    if embed and py("-m", "pip", "install", "-q", "-e", f"{spec}[embed]" if Path(spec).exists() else f"agent-system[embed] @ {spec}").returncode == 0:
        say("agent-system installed with semantic memory (fastembed)")
        return
    if embed:
        say("fastembed unavailable here: falling back to BM25 memory search")
    target = ["-e", spec] if Path(spec).exists() else [spec]
    py("-m", "pip", "install", "-q", *target).check_returncode()


def write_env(central):
    env = ROOT / ".env"
    have = env.read_text(encoding="utf-8") if env.exists() else ""
    add = ""
    if "REDIS_PASSWORD=" not in have:
        add += f"REDIS_PASSWORD={secrets.token_urlsafe(24)}\n"
    if central is not None and "ENABLE_CENTRAL_LOG=" not in have:
        add += f"ENABLE_CENTRAL_LOG={int(central)}\n"
    if add:
        env.write_text(have + ("\n" if have and not have.endswith("\n") else "") + add, encoding="utf-8")
        try:
            env.chmod(0o600)
        except OSError:
            pass
        say(f"wrote {env}")
    return {k: v for k, _, v in (l.partition("=") for l in env.read_text(encoding="utf-8").splitlines()) if k}


def compose_cmd():
    if shutil.which("docker") and run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"] if shutil.which("docker-compose") else None


def start_docker(env, central):
    cmd = compose_cmd()
    if not cmd or run(["docker", "info"], capture_output=True).returncode != 0:
        say("Docker not available: start Redis yourself and set REDIS_HOST/REDIS_PORT/REDIS_PASSWORD in .env")
        return
    run_env = {**os.environ, "REDIS_PASSWORD": env["REDIS_PASSWORD"]}
    files = ["docker-compose.yml"] + (["docker-compose-logging.yml"] if central else [])
    for f in files:
        run([*cmd, "-f", ROOT / f, "up", "-d"], env=run_env, cwd=ROOT).check_returncode()
    say("containers up: Redis" + (" + Loki/Promtail/Grafana (http://localhost:3000)" if central else ""))


def link_shims():
    """Put flow-task and agent-system on PATH. POSIX: symlinks in ~/.local/bin. Windows: print the PATH hint."""
    if WIN:
        say(f"add to PATH (PowerShell): [Environment]::SetEnvironmentVariable('Path', $env:Path + ';{BIN}', 'User')")
        return
    dest = Path.home() / ".local" / "bin"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("flow-task", "agent-system"):
        src, dst = BIN / name, dest / name
        if not src.exists():
            continue
        if dst.is_symlink() and Path(os.readlink(dst)) == src:
            continue
        if dst.exists() or dst.is_symlink():
            dst.rename(dst.with_name(name + ".bak"))
            say(f"kept your old {dst} as {name}.bak")
        dst.symlink_to(src)
        say(f"linked {dst}")
    if str(dest) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f"add {dest} to PATH")


def register_mcp():
    exe = BIN / ("agent-system.exe" if WIN else "agent-system")
    if exe.exists():
        run([exe, "install", "--target", "mcp"], check=False)  # MCP only: never rewrites your global rules files
        say("MCP server registered (restart Claude Desktop / Cursor / Antigravity). Claude Code: claude mcp add agent-system -- " + str(exe) + " mcp")


def install_service():
    py_exe = BIN / ("python.exe" if WIN else "python")
    if sys.platform.startswith("linux"):
        unit = Path.home() / ".config/systemd/user/flow-listener.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"[Unit]\nDescription=flow-task listener\nAfter=network.target\n\n[Service]\nWorkingDirectory={ROOT}\n"
                        f"ExecStart={py_exe} -m flow_engine listen\nRestart=always\nRestartSec=3\n\n[Install]\nWantedBy=default.target\n")
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", "flow-listener.service"], check=True)
    elif sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents/com.flow.listener.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
                         "<key>Label</key><string>com.flow.listener</string>\n"
                         f"<key>ProgramArguments</key><array><string>{py_exe}</string><string>-m</string><string>flow_engine</string><string>listen</string></array>\n"
                         f"<key>WorkingDirectory</key><string>{ROOT}</string>\n<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>\n"
                         f"<key>StandardOutPath</key><string>{ROOT}/logs/listener.out</string><key>StandardErrorPath</key><string>{ROOT}/logs/listener.err</string>\n"
                         "</dict></plist>\n")
        run(["launchctl", "unload", plist], capture_output=True)
        run(["launchctl", "load", plist], check=True)
    elif WIN:
        run(["schtasks", "/Create", "/F", "/TN", "FlowListener", "/SC", "ONLOGON", "/TR", f'"{py_exe}" -m flow_engine listen'], check=True)
        run(["schtasks", "/Run", "/TN", "FlowListener"], check=False)
    else:
        say("no service manager known for this OS: run `flow-task listen` yourself")
        return
    say("listener service installed and started")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent-system", help="Agent_system source dir or pip URL")
    ap.add_argument("--no-agent-system", action="store_true", help="task engine only")
    ap.add_argument("--no-embed", action="store_true", help="skip fastembed (BM25 memory only)")
    ap.add_argument("--no-docker", action="store_true")
    ap.add_argument("--no-central-log", action="store_true", help="Redis only, no Loki/Grafana")
    ap.add_argument("--service", action="store_true", help="start the listener at login")
    a = ap.parse_args()
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9+ required")

    src = None if a.no_agent_system else find_agent_src(a.agent_system)
    central = not a.no_central_log
    make_venv()
    install_packages(src, not a.no_embed)
    env = write_env(central if not a.no_docker else None)
    if not a.no_docker:
        start_docker(env, env.get("ENABLE_CENTRAL_LOG") == "1")
    for d in ("incoming_tasks/processed", "incoming_tasks/failed", "logs"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    link_shims()
    if src:
        register_mcp()
    if a.service:
        install_service()
    say("health check:")
    run([BIN / ("flow-task.exe" if WIN else "flow-task"), "doctor"])
    say("done. Try: flow-task enqueue \"summarize README.md\"" + ("" if a.service else "   (start the listener: flow-task listen, or re-run with --service)"))


if __name__ == "__main__":
    main()
