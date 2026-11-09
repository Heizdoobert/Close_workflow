---
name: closed-loop-task-engine
description: Triggers on 'task:[...]', 'Task:[...]' or 'tôi có task cho bạn:[...]'. Runs the full agentic flow - recall (agent-system), folder scan, plan via FLOW_SKILL (flow_plan_create), user review, parallel sub-agents in git worktrees, final evidence report (việc làm / file / lý do / bằng chứng).
---

# task:[...] — plan -> user review -> execute -> user review

The user only writes `task:[<nội dung>]`, waits for the plan, approves, waits again, reviews the report. Do the rest.
Skip this flow for tiny work (typo, rename, a question): do it directly.

## Steps
1. **Context (cheap, <1.2KB total)**: `agent_memory_search` (top 3) and `agent_memory_recipe_get` for the keywords; `agent_scan` on the working folder for structure. Fold only what matters into the goal text.
2. **Interview (clear the task with the user first)**: unless the task is unambiguous and self-contained, or the user said they want speed over verification, or you are in a non-interactive run (CI, /loop): use the `agent-skills:interview-me` method. Run `flow-task interview-step "<goal>" [--qa qa.json] --model <id>` (JSON: `confidence`, `hypothesis`, one `question`, your `guess`). Ask the user ONLY that question with the guess, append `{"q","a"}` to `qa.json`, call again. Stop when `brief` appears (confidence >= 95, or the round limit). Show the brief and ask "đúng chưa?". If they correct it, add the correction to `qa.json` and call again. Once they confirm, do NOT ask for another go-ahead to plan: save the brief to a file and go straight to step 3 with `--brief <file> --confirmed` (`--confirmed` only after their yes). Skipping the interview is allowed but the report then marks step 1 THIẾU.
3. **Plan**: call MCP `flow_plan_create` with `goal` = task + context, and `model` = your own exact model id (the one this CLI selected; omit it rather than guess). The MCP tool has no brief argument yet, so after an interview use the CLI: `flow-task plan "<goal>" --model <id> --brief <file> --confirmed` in the repo root. This only plans, nothing is executed.
4. **Review gate**: show the user the plan (steps, files, dependencies, acceptance command per step). STOP and wait. The user may edit `plans/<id>/plan.json`. Never approve on your own.
5. **Execute**: only after the user's OK, call `flow_plan_approve` (or `flow-task approve <id>`). Sub-agents run in parallel in their own worktrees, retry 503/429 and resume themselves. Check `flow_plan_status` at long intervals, never in a tight loop.
6. **Final review**: give the user `plans/<id>/report.md` per step as Việc làm / File / Lý do / Bằng chứng, plus the result branch `flow/<id>`. A failed step: show its evidence and the uncommitted files. Never merge into the user's branch unless asked. Show the `Độ phủ 16 bước` table and the `Quyết định SHIP` section as they are (NO-GO stays NO-GO until the blockers are fixed or the user accepts the risk): rows marked MỘT PHẦN are the model's own statement (root cause, approach, fix, spec; the interview confidence; security hardening covers only dangerous code patterns), THIẾU means that step did not run. Do not rewrite them as done.
7. **Learn**: after a failure's root cause is found, `agent_memory_recipe_record`.

Model rule: if your model is GPT-OSS 120B the sub-agents stay on GPT-OSS 120B (pinned, never another model). If it is Gemini Flash, they try GPT-OSS 120B first, then lighter. FLOW_SKILL does this from the `model` you pass.

Logs: `<FLOW_SKILL repo>/logs/` -> Loki -> Grafana `http://localhost:3000` (`{job="flow"}`).
