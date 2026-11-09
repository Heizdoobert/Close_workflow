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
git clone <FLOW_SKILL repo> FLOW_SKILL         # this repo only: Agent_system is found next to it, or cloned into vendor/
cd FLOW_SKILL
./install.sh            # Linux / macOS
```
```powershell
.\install.ps1           # Windows (PowerShell)
```

Flags (same for both): `--service` start the listener at login · `--no-docker` skip containers · `--no-central-log` Redis only, no Loki/Grafana · `--no-embed` BM25 memory only (skips the ~200 MB fastembed download) · `--agent-system PATH_OR_URL` Agent_system elsewhere (`AGENT_SYSTEM_URL` changes the clone source) · `--no-agent-system` task engine only.

What it does, in order: venv → `pip install -e` both projects → `.env` with a random Redis password → `docker compose up -d` → `flow-task` and `agent-system` on PATH (Linux/macOS: symlinks in `~/.local/bin`; Windows: prints one PATH command) → registers the MCP server in Claude Desktop, Cursor, Windsurf, Antigravity → optional listener service (systemd `--user`, launchd, Task Scheduler) → `flow-task doctor`.

It never rewrites your global rules files (`~/.claude/CLAUDE.md` etc.); to make agents follow `task:[...]` run `agent-system install --target rules` once (writes a marked block into the Gemini, Cursor, Antigravity and Claude Code rules files). When `~/.claude` exists it copies the Claude skill `skills/closed-loop-task-engine` to `~/.claude/skills/` (a different existing copy is kept as `SKILL.md.bak`). Existing `flow-task` / `agent-system` commands are kept as `*.bak`.

Claude Code CLI registers MCP servers itself; the installer prints the exact command:
`claude mcp add agent-system -- <repo>/.venv/bin/agent-system mcp`

## 2. Use

Just installed? Check it before you use it on a real project: `flow-task doctor`, then `flow-task selftest` (runs the whole plan flow once in a throwaway repo, about 0.03 USD). The full ladder is in [AGENT_SETUP.md](AGENT_SETUP.md) section 5.

```bash
flow-task doctor                         # health: Redis, listener, claude, agent-system
flow-task listen                         # run the listener in the foreground (or install with --service)
flow-task enqueue "summarize README.md"  # queue a task, prints its id
flow-task status <id>                    # status, cost, tokens, result
echo "task:[check the build]" > incoming_tasks/t.txt   # file drop works too
```

Task settings (env): `FLOW_MODEL` (haiku) · `FLOW_TOOLS` (read-only `Read,Grep,Glob`; add `Edit,Write,Bash` deliberately) · `FLOW_MAX_USD` · `FLOW_TIMEOUT` · `FLOW_WORKDIR` · `FLOW_GATE=1` run gates after the task and retry on failure (`FLOW_RETRIES`, default 2) · `FLOW_CRITIC=1` add Agent_system's critic as a gate · `FLOW_RECALL=0` / `FLOW_LEARN=0` turn memory off.

## 3. Agentic flow: give a task, review the plan, review the report

```
you -> agent (Claude / Antigravity / OpenCode) -> flow_plan_create(goal, model=<its own model id>)   -> plan (steps, files, deps, acceptance cmd)
you read the plan, say OK        -> flow_plan_approve   -> runner in the background:
        independent steps run in parallel, each in its own git worktree (flowstep/<plan>/<step>)
        step passes gates + its acceptance command -> merged into branch flow/<plan>   (your branch is never touched)
        API error 503/429/timeout -> backoff retry -> next backend;  merge conflict -> step re-run on the merged tree
        runner killed -> the listener (or `flow-task run <plan>`) resumes where it stopped
you <- flow_plan_status -> plans/<plan>/report.md : per step  Việc làm / File / Lý do / Bằng chứng
you: git diff <base>..flow/<plan>   then   git merge flow/<plan>
```

CLI equivalents: `flow-task plan "<goal>" [--model ID]` · `show <plan>` · `approve <plan> [--wait]` · `reject <plan>` · `status <plan>` · `report <plan>` · `run <plan>` · `plans`. You can edit `plans/<plan>/plan.json` before approving; approval freezes it by hash.

Step 1 is an interview (agent-skills `interview-me`): `flow-task interview "<goal>"` asks one question at a time until it is 95% sure what you want, you confirm the brief, and the plan is drafted from it (agents without a terminal use `flow-task interview-step`, then `flow-task plan "<goal>" --brief FILE --confirmed`). Every plan step also runs the `security` (hardening patterns) and `code_verify` (stubs, debug leftovers, swallowed errors, tests without assertions) gates, and a whole-diff scan of both feeds the ship decision.

The planner must also deliver an analysis (research, root cause, approach and rejected alternatives, fix, blast radius, spec: steps 1-6 of the working method); `plan.md` shows it, every step agent is briefed with it, a step may not delete or rename an undeclared path or orphan a still-used function (gate `blast_radius`), and after integration one read-only agent reviews and simplifies the merged diff (`FLOW_REVIEW=0` to skip, `FLOW_REVIEW_USD`). The last step, SHIP (from the agent-skills `/ship` command): three read-only personas (code-reviewer, security-auditor, test-engineer) run in parallel over the merged diff and the harness merges them into GO / NO-GO with blockers, recommended fixes, acknowledged risks and a rollback plan (`FLOW_SHIP=0` to skip, `FLOW_SHIP_USD`). It is advice: nothing is merged for you. `report.md` ends with a coverage table of all 16 steps that says which were checked by the harness and which are only the model's own statement.

What the report's evidence is: facts the harness collected itself (git diff of each step, gate results, the acceptance command's exit code and output, integration re-run on the merged branch, every API retry/fallback), not the agent's own claims. Agent summaries are shown next to them, clearly separate.

Limits: sub-agents work from the committed HEAD (uncommitted changes in your repo are not seen). The Claude backend limits Bash to an allow-list (`FLOW_BASH_ALLOW`); that is not a sandbox. Approval by an agent over MCP is a protocol (the instruction block tells agents to wait for your OK), not something code can verify: the hard gate is you running `flow-task approve`.

### Model policy (Antigravity)
Pass your model id when drafting (`--model`, or the MCP `model` argument, or env `FLOW_PARENT_MODEL`). `flow-task` cannot see which model the parent agent runs on, so without it the default chain `claude:haiku` is used.

| Parent model | Sub-agent chain |
| --- | --- |
| `gpt-oss-120b-medium` (GPT-OSS 120B) | `agy:<same id>` **pinned**: up to `FLOW_PIN_RETRIES` (10) persistent retries with backoff, the same agy conversation is resumed (`--conversation`), and **never another model**. A permanent error (expired key, bad model id, broken sandbox) fails loudly instead of switching. |
| `gemini-3.8-flash-*` | GPT-OSS 120B first (persistent retries), and only if it truly cannot be called: the parent's own Flash model, then `gemini-3.8-flash-low` |
| anything else / none | `FLOW_BACKENDS` (default `claude:haiku`) |

`FLOW_BACKENDS="claude:haiku,claude:sonnet,agy:gpt-oss-120b-medium,opencode"` overrides everything. Spec suffix `!` pins, `+` prefers. `--model` is always passed to agy explicitly. Tuning: `FLOW_API_RETRIES` (3), `FLOW_BACKOFF_BASE/CAP`, `FLOW_PIN_RETRIES`, `FLOW_PIN_BACKOFF_CAP`. Budgets: `FLOW_STEP_USD` (0.40), `FLOW_PLAN_MAX_USD` (3.0), `FLOW_PLAN_USD` (0.30), `FLOW_PARALLEL` (3), `FLOW_MAX_STEPS` (8).

Antigravity runs inside your `agent-sandbox` Docker, which only mounts `/mnt/second_drive` and `/tmp`: keep the repo under one of them. OpenCode: `FLOW_BACKENDS=...,opencode[:provider/model]` works only with a valid OpenCode key.

## 4. How the two halves talk

```
agent ──MCP──> agent-system ──flow_task_submit / flow_plan_*──> flow-task ──> Redis + incoming_tasks/ + plans/
                    ▲                                                               │
       memory search (recall)  ─────────────────────────────────────────────────────┤  listener / runner:
       memory index  (learn)   ─────────────────────────────────────────────────────┤  sub-agents + gates
       recipe-record (gate retry) ──────────────────────────────────────────────────┘
```

- **FLOW → Agent_system:** before each task `memory search` adds up to 1.2 KB of relevant earlier insights to the prompt; after success the result is indexed; a gate retry records a recipe (`flow-gate:<failed gates>`).
- **Agent_system → FLOW:** MCP tools `flow_task_submit`, `flow_task_status`, `flow_plan_create`, `flow_plan_approve`, `flow_plan_status`.

## 5. Update / uninstall

Update: `git pull` in both repos and re-run the installer (idempotent; keeps `.env`).
Uninstall: stop the service (`systemctl --user disable --now flow-listener` / `launchctl unload ~/Library/LaunchAgents/com.flow.listener.plist` / `schtasks /Delete /TN FlowListener /F`), `docker compose down`, delete the folders and `~/.local/bin/{flow-task,agent-system}`. Memory lives in `~/.agent_system/` (set `AGENT_SYSTEM_DB_DIR` to move it).

## 6. Troubleshooting

`flow-task doctor` names the failing piece. Redis `WRONGPASS` → the container was created with an older `.env`: `docker compose down -v` then re-run the installer. No `claude` found → install Claude Code or set `FLOW_CLAUDE`. Windows: allow script execution with `Set-ExecutionPolicy -Scope Process Bypass` if PowerShell blocks `install.ps1`.
