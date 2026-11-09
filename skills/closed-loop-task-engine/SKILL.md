---
name: closed-loop-task-engine
description: Triggers on 'task:[...]', 'Task:[...]' or 'tôi có task cho bạn:[...]'. Runs the full agentic flow - recall (agent-system), folder scan, plan via FLOW_SKILL (flow_plan_create), user review, parallel sub-agents in git worktrees, final evidence report (việc làm / file / lý do / bằng chứng).
---

# task:[...] — plan -> user review -> execute -> user review

The user only writes `task:[<nội dung>]`, waits for the plan, approves, waits again, reviews the report. Do the rest.
Skip this flow for tiny work (typo, rename, a question): do it directly.

## Steps
1. **Context (cheap, <1.2KB total)**: `agent_memory_search` (top 3) and `agent_memory_recipe_get` for the keywords; `agent_scan` on the working folder for structure. Fold only what matters into the goal text.
2. **Plan**: call MCP `flow_plan_create` with `goal` = task + context, and `model` = your own exact model id (the one this CLI selected; omit it rather than guess). No MCP: run `flow-task plan "<goal>" --model <id>` in the repo root. This only plans, nothing is executed.
3. **Review gate**: show the user the plan (steps, files, dependencies, acceptance command per step). STOP and wait. The user may edit `plans/<id>/plan.json`. Never approve on your own.
4. **Execute**: only after the user's OK, call `flow_plan_approve` (or `flow-task approve <id>`). Sub-agents run in parallel in their own worktrees, retry 503/429 and resume themselves. Check `flow_plan_status` at long intervals, never in a tight loop.
5. **Final review**: give the user `plans/<id>/report.md` per step as Việc làm / File / Lý do / Bằng chứng, plus the result branch `flow/<id>`. A failed step: show its evidence and the uncommitted files. Never merge into the user's branch unless asked.
6. **Learn**: after a failure's root cause is found, `agent_memory_recipe_record`.

Model rule: if your model is GPT-OSS 120B the sub-agents stay on GPT-OSS 120B (pinned, never another model). If it is Gemini Flash, they try GPT-OSS 120B first, then lighter. FLOW_SKILL does this from the `model` you pass.

Logs: `<FLOW_SKILL repo>/logs/` -> Loki -> Grafana `http://localhost:3000` (`{job="flow"}`).
