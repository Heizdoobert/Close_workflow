# Bench: open items

State: `main` at `3a86de8`, deployed 2026-11-17. Full pytest 705 passed, and 705 passed inside the sandbox (the way a claude step agent runs it), with no signing error.
Update this list when an item is done (delete the line, note the commit).

## Open

- [ ] Saturated bench: haiku solves all five tasks in every normal run, so no change can show a flip. Write 2-3 harder tasks and confirm with a real run that they fail at baseline.
- [ ] `analyze` label `[turn T, D denied]` counts D over the whole task, not turn T (phase 3 review, low).
- [ ] Without a manifest, `compare` still writes every regression to `regressions_unexpected` (phase 3 review, low).
- [ ] `compare` reads each `results.json` twice (`_rewards` and `_load_results`) (phase 3 review, low).
- [ ] agy: not checked whether commits work inside its own container (no bwrap, no `~/.gitconfig` mounted).
- [ ] `harness/runs/` is not committed (not git-ignored either), so there is no committed baseline. Decide: commit a run or keep runs local.

## Won't do (reason)

- `test_restore_refuses_paths_outside_cwd` as `parametrize`: changing its `def test_` line counts as a removed test in the constraints gate.
- Drop the `step` field from trace events: test names pin it (`test_stream_step_is_caller_step_...`); `analyze` prints the turn instead.
- `bench.py` in `COMPONENTS`: on purpose, the loop must not edit its own measure (now in `CLAUDE.md` and `AGENT_SETUP.md`).

## Phase 3 (2026-11-17): done

Plan `20261117-105632-3bf9` (5 steps, all passed on attempt 1, SHIP GO, 0.18 USD) plus two review fixes (`3041c53`, `3a86de8`).
- Sandbox: `sandbox._no_signing` binds `<FLOW_HOME>/logs/sandbox-gitconfig-<hash>` (the global git config bytes plus `commit.gpgsign=false`, `tag.gpgsign=false`) over `~/.gitconfig`. Pytest inside the sandbox: 25 failed + 129 errors before, 0 after. On the deployed copy a sandboxed `git commit` returns 0 (was 128) and `~/.ssh` still shows 0 entries.
- Trace: each tool event gets `error` (redacted tool result text, 120 chars) and `denied` (in `permission_denials`, or "No such tool available").
- Bench: the copy keeps `.env.example`; results carry `turns` and `denied`; `change` takes `--predict none` and `--risk`; `compare` prints cost and turn totals and expected/unexpected regressions, writes them to `eval.json`, warns about an incomplete newer run and no longer prints `kept run N changes`; `analyze` prints `[turn N, D denied]`, counts a refused call in the last turn as `backends`, and says when the bench is saturated.
- Review fixes: a `~/.gitconfig` that is not UTF-8 crashed `wrap()` (now bytes); the `.tmp-<pid>` name raced between plan steps, which are threads of one process (now `mkstemp`, and an existing copy is not rewritten); a cost drop printed `(+-25.0%)`.

Real run with the deployed engine (lab run 6): 5/5, 0.0298 USD (-3.9% against run 5), `turns n/a` because run 5 has no turns. The trace now shows what phase 2 could not see: greet's `PYTHONPATH=. python3 -I -c ...` was refused ("This command requires approval", `denied`), so an env prefix in front of `python3` does not match `Bash(python3:*)`; a failing Bash command carries `error` ("Exit code 1 ..."); analyze-json had one refusal from Claude's own command check ("Contains brace with quote ...").

## Phase 2 (2026-11-17): the AHE loop on real data

Lab clone of `main` plus three new tasks (`analyze-json`, `slack-redact`, `plans-status`; each fails on the unmodified code, now in `main` as `2450970`), `claude:haiku`, run as `python -m flow_engine bench ...` from the lab.

| Run | Change (manifest) | Reward | Cost USD | `compare` |
|---|---|---|---|---|
| 1 | none (baseline) | 5/5 | 0.027 | first run |
| 2 | real fix: `secrets.agent_env` turns signing off | 5/5 | 0.028 | restored: zero predicted flips |
| 3 | experiment: no `Edit` or `Write` in claude `--tools` | 5/5 | 0.034 | restored: zero predicted flips |
| 4 | experiment: `--tools Read,Glob,Grep` | 0/5 | 0.024 | 5 regressions; restored: regressions 5 > flips 0 |
| 5 | none (after the restore) | 5/5 | 0.031 | 5 flips 0->1, nothing to restore |

Seen on real data: regressions, flips and both restore rules; every restore left `git diff` clean. Not seen: a change that `compare` keeps, because the bench is saturated.

## Security notes

- The tool list and `BASH_ALLOW` do not limit writes. In run 3 (no `Edit`, no `Write`) agents wrote files with `python3 - <<EOF` and with `printf ... >`. Claude also runs read-only commands that are not in `--allowedTools` (probe: `uname -s` ran with `--allowedTools "Bash(ls:*)"`). The real write limit is bwrap: the worktree, `/tmp` and claude's config. The network stays open.
- Agents write helper scripts into Claude's own scratchpad under `/tmp/claude-1000/` and run them with `python3`.

## Known limits (no fix planned)

- bwrap does not block the network; `accept.sh` can reach the net inside the sandbox.
- `claude -p` has no `--max-turns`, so a task's turn count cannot be capped.
- SHIP rows `MỘT PHẦN`: the harness cannot check that a root cause or spec is true (analysis has no `file:line` citations).
- MCP `flow_task_submit` does not pass a workdir.
