# FLOW_SKILL — chi tiết kiến trúc (chuyển từ ~/.claude/CLAUDE.md)

## Tri-Modal Token Economics
- **Tier 0 (0-token)**: Code thuần, DuckDB local, streaming RFC 4180 `QuoteAwareChunker`.
- **Tier 1 (Real-time LLM)**: SLA < 12h, interactive loops, code direct edit.
- **Tier 2 (Batch API -50% cost)**: >5M tokens, batch offline không khẩn cấp.

## Resilient Distributed Queue
- Celery invariants: `task_acks_late=True`, `visibility_timeout=86400`, `worker_prefetch_multiplier=1`.
- Circuit Breaker: tự ngắt & retry an toàn, chống silent data loss.

## 3-Tier Memory Layer
- **Tier 1 (Vector)**: Milvus Lite 3.0.2 + BLAKE3 UID + SourceChunkPointer.
- **Tier 2 (Storage)**: Knowledge Base / Object Store + Hybrid Search (BM25 + Dense).
- **Tier 3 (Graph/Relational)**: SQLiteStore WAL (`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`).

## Closed-Loop Task Engine (đầy đủ)
1. **Persistence**: `/usr/local/bin/flow-task "<nội dung>"` -> Redis `localhost:6379`, key `task:<timestamp>` (TTL 30 ngày, field `status`).
2. **Observability**: log vào `/mnt/second_drive/FLOW_SKILL/logs/` -> Promtail -> Loki -> Grafana `http://localhost:3000`, nhãn `job="flow"`.
3. **Pipeline đầy đủ** (chỉ cho task tính năng/sửa lỗi lớn): `spec -> plan -> build -> test -> review -> constraints -> code-verification -> simplify -> security -> ship`, thẩm định Judge/Critic/Prover.

## Cross-OS engine + agent-system wiring (Phase 7)
- Engine = stdlib Python package `flow_engine/` (`flow-task` console script; `run_task.sh`/`listener.sh` are shims). Install on any OS: `./install.sh` or `.\install.ps1` -> `install.py` (see INSTALL.md). `flow-task doctor` = health.
- `flow-task enqueue "<msg>"` stores + queues for the listener; `status <id>` reads `taskstate:<id>`. Listener polls `incoming_tasks/` (file lock = single instance).
- Task run: recall (`agent-system memory search`, <=1.2KB into prompt) -> `claude -p` haiku -> gates if `FLOW_GATE=1` (retry with gate output, `FLOW_RETRIES`, one `FLOW_MAX_USD`; `FLOW_CRITIC=1` adds Agent_system critic) -> on retry `recipe-record flow-gate:<names>` -> on success `memory index`. Turn off with `FLOW_RECALL=0` / `FLOW_LEARN=0`.
- Agent -> FLOW: MCP tools `flow_task_submit` / `flow_task_status` in Agent_system.
- Memory: SQLite `~/.agent_system/agent_system.db` (`AGENT_SYSTEM_DB_DIR`), fastembed vectors + BM25. `AGENT_SYSTEM_LLM_PATCH=1` = haiku patcher for `reflect`.

## Agentic plan flow + model policy (Phase 8)
- `flow-task plan "<goal>" [--model ID]` -> review -> `approve` -> parallel sub-agents in git worktrees (`flowstep/<plan>/<step>`), merged into `flow/<plan>` after gates + acceptance; `plans/<plan>/report.md` = Việc làm / File / Lý do / Bằng chứng. MCP: `flow_plan_create|approve|status`. Code: `flow_engine/plan.py`, `flow_engine/agents.py`.
- Backends: claude, agy, opencode. 503/429/timeouts: backoff retry, then next backend. Parent `gpt-oss-120b-medium` => pinned (`agy:<id>!`): persistent retries, same conversation, never another model. Parent `gemini-3.8-flash-*` => GPT-OSS 120B first, then lighter. `FLOW_BACKENDS` overrides. Pass the parent's own model id; flow-task cannot detect it.
- Reflection: a step that fails gates/acceptance is retried with the failure output plus any stored recipe (`flow-gate:<names>`); the lesson is recorded (`fixed after` always, `still failing` only if no recipe exists). Shown in report.md as `reflection:`.
- Many sessions: a plan targets the git repo of the caller's cwd (`$FLOW_WORKDIR` overrides) and stores it in plan.json, so approving from another folder keeps it. Plans are isolated (own ids, worktrees, branches `flow/<id>`); 12 concurrent plans on one repo tested. Two plans touching the same files conflict only when you merge their branches into yours.
- Sandbox: `flow_engine/sandbox.py` wraps each claude sub-agent in bwrap (`sandbox.wrap`): read-only `/`, writable only the step worktree, `/tmp` and claude's config. Without a working bwrap, one warning is logged and the `escaping_paths` post-check after each step is the only guard. agy is not wrapped (its own sandbox mounts only /mnt/second_drive and /tmp). `flow-task doctor` shows the sandbox line.
- Acceptance discipline: `plan.validate()` rejects an empty or trivially-true acceptance command (`true`, `:`, `exit N`, `echo`/`printf` only). The constraints gate fails a step that deletes a test file, removes an assert or adds a skip/xfail/suppression marker (`tests/test_acceptance_gate.py`).
- Secrets fail closed: the secrets gate runs gitleaks; when gitleaks is missing it runs the built-in regex scan (`flow_engine/secrets.py` `SECRET_PATTERNS`) over the step's added lines and secret file names, and fails on a hit without echoing it. `flow-task doctor` reports gitleaks missing as a warning. Tests: `tests/test_secrets_gate.py`.
- `flow-task recover`: finds plans in running/approved with a dead runner (stale heartbeat, no live lock or pid) and resumes them without the listener. It also kills the orphan agent process groups recorded in `state.json` (`agent_pgids`). On Linux each agent gets `PR_SET_PDEATHSIG` (ctypes), so it dies with its runner. macOS relies on `recover`. Tests: `tests/test_recover.py`.
- Overlap warning: `approve` warns (never blocks) when another live plan on the same repo lists the same files. Merging both branches can still conflict.
- `flow-task clean [--older-than DAYS] [--result-branches]`: removes finished plans' worktrees and `flowstep/*` branches. Keeps `flow/<pid>` unless `--result-branches`, never touches a running plan. Tests: `tests/test_clean.py`.
- Reflection journal: each attempt goes to `plans/<pid>/journal/<sid>.jsonl` (redacted). A retry or resume is told the earlier attempts. The same failure 3 times in a row ends the step as `stuck`. A file outside the plan's `files` is a drift warning (the merge still goes ahead). `STATUS.md` in the plan dir and `flow-task where [<plan>]` give the state and next action in one short text. Tests: `tests/test_reflection_journal.py`.
- 12-step method (`flow_engine/method.py`): the planner must return an `analysis` block (research citing files that exist at the base commit, root_cause, approach + rejected alternatives, fix, blast_radius, spec); `create` re-asks until it is complete, each step agent gets a <=1.1KB brief of it. Gate `blast_radius` (step runs only): no undeclared deleted or renamed path, no removed top-level def/class still referenced. After integration one read-only agent call reviews and simplifies the merged diff (`FLOW_REVIEW=0` off, `FLOW_REVIEW_USD` 0.15, `FLOW_REVIEW_BYTES` 30000); advisory, findings about files outside the diff are dropped. `report.md` has the analysis and an `Độ phủ 12 bước` table (OK / MỘT PHẦN / THIẾU). The harness cannot verify that a root cause or spec is true: those rows say `MỘT PHẦN`. Tests: `tests/test_twelve_steps.py`.
- Known: OpenCode key expired (401) as of 2026-11-09; agy sandbox only mounts /mnt/second_drive and /tmp.
