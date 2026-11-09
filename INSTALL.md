# Install

Works on Linux, macOS and Windows. Needs Python 3.9+ and, for Redis/Grafana, Docker. Nothing else: the engine is stdlib Python (no `redis-cli`, no `inotify`, no bash on Windows).

Two projects, one system:

| Project | Role |
| --- | --- |
| **FLOW_SKILL** (this repo) | task queue (Redis), cheap `claude -p` worker, scripted gates, Loki/Grafana logs |
| **Agent_system** (`../Agent_system`) | memory (SQLite + embeddings), critic/reflection, MCP server |

`install.py` installs both into one venv (`.venv`) and wires them together.

## 1. Install

```bash
git clone <FLOW_SKILL repo> FLOW_SKILL
git clone <Agent_system repo> Agent_system     # sibling folder, found automatically
cd FLOW_SKILL
./install.sh            # Linux / macOS
```
```powershell
.\install.ps1           # Windows (PowerShell)
```

Flags (same for both): `--service` start the listener at login · `--no-docker` skip containers · `--no-central-log` Redis only, no Loki/Grafana · `--no-embed` BM25 memory only (skips the ~200 MB fastembed download) · `--agent-system PATH_OR_URL` Agent_system elsewhere.

What it does, in order: venv → `pip install -e` both projects → `.env` with a random Redis password → `docker compose up -d` → `flow-task` and `agent-system` on PATH (Linux/macOS: symlinks in `~/.local/bin`; Windows: prints one PATH command) → registers the MCP server in Claude Desktop, Cursor, Windsurf, Antigravity → optional listener service (systemd `--user`, launchd, Task Scheduler) → `flow-task doctor`.

It never rewrites your global rules files (`~/.claude/CLAUDE.md` etc.). Existing `flow-task` / `agent-system` commands are kept as `*.bak`.

Claude Code CLI registers MCP servers itself; the installer prints the exact command:
`claude mcp add agent-system -- <repo>/.venv/bin/agent-system mcp`

## 2. Use

```bash
flow-task doctor                         # health: Redis, listener, claude, agent-system
flow-task listen                         # run the listener in the foreground (or install with --service)
flow-task enqueue "summarize README.md"  # queue a task, prints its id
flow-task status <id>                    # status, cost, tokens, result
echo "task:[check the build]" > incoming_tasks/t.txt   # file drop works too
```

Task settings (env): `FLOW_MODEL` (haiku) · `FLOW_TOOLS` (read-only `Read,Grep,Glob`; add `Edit,Write,Bash` deliberately) · `FLOW_MAX_USD` · `FLOW_TIMEOUT` · `FLOW_WORKDIR` · `FLOW_GATE=1` run gates after the task and retry on failure (`FLOW_RETRIES`, default 2) · `FLOW_CRITIC=1` add Agent_system's critic as a gate · `FLOW_RECALL=0` / `FLOW_LEARN=0` turn memory off.

## 3. How the two halves talk

```
agent (Claude / Cursor / Antigravity) ──MCP──> agent-system ──flow_task_submit──> flow-task enqueue ──> Redis + incoming_tasks/
                                                    ▲                                                        │
                       memory search (recall)  ─────┤                                                  listener runs
                       memory index  (learn)   ─────┤                                              haiku worker + gates
                       recipe-record (gate retry)───┴───────────────────────────────────────────────────────┘
```

- **FLOW → Agent_system:** before each task `memory search` adds up to 1.2 KB of relevant earlier insights to the prompt; after success the result is indexed; a gate retry records a recipe (`flow-gate:<failed gates>`).
- **Agent_system → FLOW:** MCP tools `flow_task_submit` and `flow_task_status` hand work to the cheap worker and read the outcome.

## 4. Update / uninstall

Update: `git pull` in both repos and re-run the installer (idempotent; keeps `.env`).
Uninstall: stop the service (`systemctl --user disable --now flow-listener` / `launchctl unload ~/Library/LaunchAgents/com.flow.listener.plist` / `schtasks /Delete /TN FlowListener /F`), `docker compose down`, delete the folders and `~/.local/bin/{flow-task,agent-system}`. Memory lives in `~/.agent_system/` (set `AGENT_SYSTEM_DB_DIR` to move it).

## 5. Troubleshooting

`flow-task doctor` names the failing piece. Redis `WRONGPASS` → the container was created with an older `.env`: `docker compose down -v` then re-run the installer. No `claude` found → install Claude Code or set `FLOW_CLAUDE`. Windows: allow script execution with `Set-ExecutionPolicy -Scope Process Bypass` if PowerShell blocks `install.ps1`.
