# Evidence, 2026-11-09 (real runs, nothing mocked unless stated)

| File | What it proves |
| --- | --- |
| real-claude-haiku-2steps.md | claude haiku planned a 2-step goal, ran both steps in git worktrees, merged, integration re-run OK ($0.0059) |
| real-agy-gpt-oss-120b-pinned.md | Antigravity CLI with `--model gpt-oss-120b-medium` planned and ran 2 parallel steps (started 0.04 s apart), merged, integration OK |
| real-mcp-claude-haiku-drives-flow.md | a real LLM called the MCP tools `flow_plan_create` -> `flow_plan_approve` -> `flow_plan_status`; the plan ran to done |

Other real observations (not reproducible as files):
- `opencode run` returned `401 This API key has expired`: classified permanent, chain fell to `agy:gpt-oss-120b-medium` which answered.
- `agy -p --conversation <id>` continued the same chat (remembered a secret word): the resume-on-error path relies on this.
- Gemini-Flash parent: first call went to `agy:gpt-oss-120b-medium` (6 s, ok).
- A first agy attempt from `/tmp/claude-.../` failed: the agent-sandbox container does not mount that path (only /mnt/second_drive and /tmp). With a 10-retry pin this wasted 4m50s before the permanent-error pattern was added.
- A first agy plan chose bare `pytest`, which is broken on this host (`ImportError _console_main`): the harness acceptance run FAILED the step although the agent claimed success. The planner prompt now says `python3 -m pytest`.

Injected faults (fake claude/agy binaries, tests/): 503 storms, odd errors, dead keys, hard errors, merge conflicts, failing acceptance, a runner killed with SIGKILL mid-step and resumed. A real 503 from a provider was never observed in these runs.
