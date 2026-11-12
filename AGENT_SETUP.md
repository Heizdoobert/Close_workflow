# AGENT_SETUP: install FLOW_SKILL + Agent_system with no manual step

**Audience: an AI agent** (Claude Code, Antigravity, OpenCode, Cursor...). The user said "set this up". Read this file once, do the steps in order, run every check, then send the report at the bottom. Do not ask the user to run commands, with the single exception in step 2.

Result you must reach: `flow-task` and `agent-system` run from any folder, the MCP server `agent-system` is registered, the `task:[...]` protocol is in every agent's rules file, the Claude skill `closed-loop-task-engine` is installed, Redis is up, `flow-task doctor` shows no `FAIL`.

## 0. Rules for you
- Never print or copy `.env` or any password, token or API key. Never push, never change git remotes, never edit a rules file outside the `<!-- agent-system:begin -->`...`<!-- agent-system:end -->` block.
- Spend tokens only where step 5 says so (levels 1 to 3, about 0.04 USD in total). Never approve a plan on the user's real project without their OK.
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

## 5. Test before you adopt it (a ladder: stop at the first FAIL, fix it, do not skip a level)
Do this once on the new machine, before you use the system on a real project.

| Level | Command | Cost | Pass means |
|---|---|---|---|
| 0 | `.venv/bin/pip install pytest && .venv/bin/python -m pytest -q` (Windows: `.venv\Scripts\python`) | 0, about 2 min | `N passed`, no `failed`. Fake Redis and fake agents, so no network and no tokens. POSIX-only tests are skipped on Windows: say so |
| 1 | `flow-task "smoke test: answer OK, change no file"` prints `Task stored in Redis with ID <id>`; then `flow-task exec <id> "smoke test: answer OK, change no file"` | about 0.002 USD | `done task <id> (1 attempt(s)...)`, and `flow-task status <id>` shows `"status": "done"` |
| 2 | `flow-task selftest` | about 0.03 USD, 1 to 2 min | every line `OK` and `selftest PASSED` |
| 3 | `flow-task interview-step "make the thing faster"` | about 0.002 USD | JSON with `confidence` below 95, one `question`, a `guess`, and `coverage` naming 7 items (who, why, impact, success, constraints, non_goals, risks) |

- Level 0 proves the code. Level 1 proves `claude`, Redis and the listener path. Level 2 runs the real flow once in a throwaway git repo: planner, three parallel steps in git worktrees, the gates, the merge, the integration re-run, the review, the SHIP personas and the report. It also proves git worktrees work between the temp dir and this install folder (two filesystems on many machines). Level 3 proves the interview. Run level 2 unless the user said to spend nothing; then say it was skipped.
- A `FAIL` line names what broke. `every step merged` FAIL: read `flow-task selftest --keep` output, then `plans/<id>/report.md` (kept by `--keep`) and `plans/<id>/journal/`. `plan drafted` FAIL: the planner could not return valid JSON, look at `claude` itself (`claude -p "say OK"`).
- Do not touch the user's real repo during levels 0 to 3: selftest builds its own repo and removes it afterwards.

Then adopt it step by step on the real project:
1. Pick **one** small, low-risk task. Check `git status` is clean and the work is committed: sub-agents only see the committed HEAD.
2. `flow-task interview "<task>"` (needs a terminal) or, with no terminal, `flow-task interview-step` and ask the user each question yourself (see section 6). Then draft the plan. Show the user the plan: steps, files, dependencies, acceptance command per step, and the analysis. Nothing has run yet.
3. Only after the user's OK: `flow-task approve <id>` (or MCP `flow_plan_approve`). Check `flow-task where` at long intervals.
4. Give the user `plans/<id>/report.md` as it is. Read the `Độ phủ 16 bước` table with them: `MỘT PHẦN` rows are the model's own statement, `THIẾU` means that step did not run, `NO-GO` stays NO-GO until the blockers are fixed or the user accepts the risk. The branch is `flow/<id>`; never merge it for the user.
5. Only after that first task came out right, use it for more projects. Keep `FLOW_PLAN_MAX_USD` (default 3.0) and `FLOW_STEP_USD` (0.40) as the cost ceiling.

## 6. How to use it (what you need to know to operate it)
The user writes `task:[what they want]`. You then follow the skill `closed-loop-task-engine` (installed in step 2): recall memory, interview, plan, show the plan and wait, approve on their OK, report.

The 16 steps the system runs (and where they live): 1 interview, 2 research, 3 root cause, 4 how it can be done, 5 how to fix it, 6 blast radius, 7 spec (2 to 7 are the `analysis` block the planner must write), 8 plan, 9 build (parallel agents in git worktrees), 10 test (gates + each step's acceptance command + a re-run on the merged branch), 11 security hardening, 12 review, 13 simplify, 14 code-verify, 15 report, 16 SHIP (GO / NO-GO with a rollback plan). Steps 11 and 14 run as gates on every step and once over the whole diff.

Interview without a terminal (an agent):
```
flow-task interview-step "<goal>" --qa qa.json        # one JSON round: confidence, coverage, hypothesis, question, guess, brief
```
Ask the user only the `question` (with the `guess`), append `{"q": "...", "a": "..."}` to `qa.json` (write `(chấp nhận đoán) <guess>` if they only accepted the guess), call again. When `brief` appears, show it and ask "đúng chưa?". After an explicit yes: save the brief to a file and run `flow-task plan "<goal>" --brief brief.json --confirmed`. `--confirmed` only after their yes.

Commands you will use: `flow-task where` (state and next action), `show <plan>`, `approve <plan> [--wait]`, `retry <plan>` (failed steps run again), `report <plan>`, `reject <plan>`, `recover` (resume plans whose runner died), `clean` (remove finished plans' worktrees), `doctor`, `selftest`.

## 7. Limits you must tell the user (do not hide them)
- Root cause, approach, fix and spec are written by the model; the harness only checks that they are there and that cited research files exist. Their rows say `MỘT PHẦN`.
- Interview confidence is the agent's own number. The code does reject 95+ while one of the 7 items is unknown, and it lists every answer the user only accepted as an assumption. It cannot tell whether a person really answered.
- Security hardening scans for dangerous code patterns only: auth, rate limits, SSRF and resource ownership are not checked automatically. Review and SHIP are advisory and use the same model family that wrote the code.
- The blast-radius gate sees paths and top-level Python functions and classes, not methods or signatures.
- MCP `flow_plan_create` has no `brief` argument: after an interview use the CLI.
- Sub-agents see committed code only. The Claude sandbox needs bwrap (Linux); without it one warning is logged. agy is not sandboxed. Not yet tested on this OS if it is Windows or macOS: say so.

## 8. Report to the user (short)
- Checks run and their result (paste the `flow-task doctor` lines and the test ladder results: pytest count, the level 1 cost, the `selftest` lines, the interview-step check; say which level you skipped and why).
- What was installed and where (repo path, venv, which clients got MCP and rules).
- What the user must do, if anything: restart Claude Code / Claude Desktop / Cursor / Antigravity so the MCP server loads; anything you could not install without admin rights.
- How to use it: write `task:[what you want]` in an agent; it plans, shows the plan, waits for the user's OK, then runs in git worktrees and returns `plans/<id>/report.md` plus branch `flow/<id>`.
- Not verified on this OS: say so if the OS is Windows or macOS and anything above failed or was skipped.

## API mode (optional, off unless you set a key)
- CLI mode stays the default. A chain entry `api:anthropic/<model>` or `api:openai/<model>` uses an API key instead of a CLI login.
- Env vars (set the value yourself, never paste a key into a file or a task): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `FLOW_API_BASE_URL` (optional endpoint override), `FLOW_PRICE_<MODEL>` (your own price per million tokens; without it the cost is unknown, never 0), `FLOW_POLICY_API_<ROLE>` (optional extra rung), `FLOW_API_CACHE=1` (retry cache, off by default).
- A missing key means the chain falls through to the next backend; it is not an error that stops the plan.
- **Limit:** the tool loop does not exist yet. API mode serves only calls that set `prompt_only`: the review pass, the three SHIP personas and the interview round. Planner, map, scout and steps stay on CLI, and a call outside those roles is refused with 'invalid argument: api backend only serves prompt-only calls'.
- `https` is required for `FLOW_API_BASE_URL` except for localhost, 127.0.0.1 and ::1. Redirects are refused, so the key is never re-sent to another host.
- `FLOW_API_MAX_BYTES` caps the response body (default 4000000); a larger body is a permanent error.
- `FLOW_API_CACHE_MIN_CHARS` (default 4000): a longer prompt is sent with Anthropic `cache_control`. The saving is not measured.
- Tokens are the unit of cost. `FLOW_PLAN_MAX_TOKENS` sets a per-plan billable-token budget (0 = off); `FLOW_BUDGET_CACHE_READ=1` counts cache reads; `FLOW_TOKEN_CHARS` is the rough chars-per-token estimate (not exact). USD is optional through `FLOW_PRICE_<MODEL>`. The plan report has a `## Token tổng` section.
- `flow-task doctor` shows which modes are usable without printing a key.
