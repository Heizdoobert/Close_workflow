# AGENT_SETUP: install FLOW_SKILL + Agent_system with no manual step

**Audience: an AI agent** (Claude Code, Antigravity, OpenCode, Cursor...). The user said "set this up". Read this file once, do the steps in order, run every check, then send the report at the bottom. Do not ask the user to run commands, with the single exception in step 2.

Result you must reach: `flow-task` and `agent-system` run from any folder, the MCP server `agent-system` is registered, the `task:[...]` protocol is in every agent's rules file, the Claude skill `closed-loop-task-engine` is installed, Redis is up, `flow-task doctor` shows no `FAIL`.

## 0. Rules for you
- Never print or copy `.env` or any password, token or API key. Never push, never change git remotes, never edit a rules file outside the `<!-- agent-system:begin -->`...`<!-- agent-system:end -->` block.
- Do not run a paid task (`flow-task plan`, `enqueue`) to "test". The checks below cost nothing.
- Work from the repo root (the folder holding `install.py`). If the repo is not cloned yet: `git clone https://github.com/Heizdoobert/Close_workflow.git FLOW_SKILL` and `cd FLOW_SKILL`.

## 1. Detect
Run and note the results:
```
python3 --version || python --version || py --version     # need 3.9+
git --version
docker --version && docker info                            # optional: Redis/Grafana containers
claude --version                                           # Claude Code CLI, optional
```
- Python or git missing: install them with the OS package manager if you can without admin rights (`winget install Python.Python.3.12 Git.Git`, `brew install python git`, `apt install python3 python3-venv git`). If it needs admin or a GUI, stop and tell the user exactly what to install (the only allowed manual step).
- No Docker: Redis is still required at `localhost:6379`. Check `docker` first; else look for a running Redis; else tell the user to install Docker Desktop (or a Redis) and re-run you. Then add `--no-docker` only when a Redis is reachable.

## 2. Install (one command)
Linux / macOS:
```
python3 install.py --full
```
Windows (PowerShell):
```
py install.py --full
```
Useful extras: `--service` (listener starts at login), `--no-embed` (skip the ~200 MB fastembed download), `--no-central-log` (Redis only, no Loki/Grafana), `--no-docker`, `--agent-system PATH_OR_URL`.

`--full` does: venv -> install both projects -> `.env` with a random Redis password -> containers -> `flow-task`/`agent-system` on PATH (Windows: persisted in the user PATH) -> MCP registered in Claude Desktop, Cursor, Windsurf, Antigravity and Claude Code (user scope) -> the `task:[...]` block written into the Gemini, Cursor, Antigravity, Claude Code and OpenCode rules files -> the Claude skill copied to `~/.claude/skills/closed-loop-task-engine/`.

A new terminal is needed for PATH (Windows). If `flow-task` is not found, call it by path: `.venv/bin/flow-task` (Windows: `.venv\Scripts\flow-task.exe`).

## 3. Verify (all must pass)
```
flow-task doctor            # no FAIL. "warn" lines (listener, sandbox, gitleaks) are allowed
agent-system doctor         # ok
flow-task plans             # runs without error (empty list is fine)
```
Rules blocks present (each file you expect to exist must contain the marker):
```
grep -l "agent-system:begin" ~/.claude/CLAUDE.md ~/.gemini/config/rules/AGENTS.md ~/.cursorrules ~/.agent/rules.md ~/.config/opencode/AGENTS.md
```
(Windows: `Select-String -Path "$HOME\.claude\CLAUDE.md","$HOME\.gemini\config\rules\AGENTS.md" -Pattern "agent-system:begin"`.) A file that does not exist belongs to an agent the user does not have: fine. Skill: `~/.claude/skills/closed-loop-task-engine/SKILL.md` exists if `~/.claude` exists. Claude Code: `claude mcp list` shows `agent-system`.

Listener (needed for `flow-task enqueue`, not for `task:[...]` plans): `flow-task listen` stays in the foreground; re-run step 2 with `--service` to keep it running at login.

## 4. If a check fails
| Symptom | Fix |
|---|---|
| `redis` FAIL `WRONGPASS` | the container was made with an older `.env`: `docker compose down -v`, re-run step 2 |
| `redis` FAIL connection refused | `docker compose up -d`, or start your own Redis; check `REDIS_HOST`/`REDIS_PORT` in `.env` |
| `claude` FAIL | install Claude Code, or set `FLOW_CLAUDE` to its path; the engine needs it for the default backend |
| `agent-system` missing | Agent_system was not found or cloned: re-run with `--agent-system PATH_OR_URL` |
| PowerShell blocks `install.ps1` | `Set-ExecutionPolicy -Scope Process Bypass`, then run `py install.py --full` |
| `flow-task: command not found` (POSIX) | add `~/.local/bin` to PATH, or use `.venv/bin/flow-task` |

Re-running `install.py --full` is safe: existing `flow-task`/`agent-system` commands are kept as `*.bak`, a different skill copy as `SKILL.md.bak`, and only the marked blocks in rules files are rewritten.

## 5. Report to the user (short)
- Checks run and their result (paste the `flow-task doctor` lines).
- What was installed and where (repo path, venv, which clients got MCP and rules).
- What the user must do, if anything: restart Claude Code / Claude Desktop / Cursor / Antigravity so the MCP server loads; anything you could not install without admin rights.
- How to use it: write `task:[what you want]` in an agent; it plans, shows the plan, waits for the user's OK, then runs in git worktrees and returns `plans/<id>/report.md` plus branch `flow/<id>`.
- Not verified on this OS: say so if the OS is Windows or macOS and anything above failed or was skipped.
