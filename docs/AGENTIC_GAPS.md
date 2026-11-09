# Agentic plan flow: gap list (prioritized)

Scope: `flow_engine/plan.py`, `agents.py`, `gate.py`, `bridge.py`, `engine.py`, `sandbox.py`, `secrets.py`, `journal.py`, `cli.py` and `tests/`, as of step s6 (base `fd08d93`, merged s0–s5, s6 = these docs).

How this list was built:
- Each item was checked by reading the code at the cited line. Line numbers refer to the base tree (`fd08d93`) and are not updated.
- Items marked **(probe)** were reproduced by a scratch script that is NOT committed. Items marked **(by reading)** were not run.
- Baseline: `python3 -m pytest -q tests` = 88 passed when this list was first written (base `fd08d93`). After s0–s5 the suite had 161 tests; with s6 (`tests/test_docs.py`, which checks that every test name cited here exists) it is 165 passed.
- Status tags: **FIXED** = a test named here proves it; **OPEN** = not done; **PARTIAL** = the residual is stated in the item.
- No real LLM, claude or agy call was made in this step. Nothing was spent from the plan budget.

Fixed already (for reference, each has a test in `tests/`): secret masking in state, reports and recipes (`test_secret_in_agent_summary_and_acceptance_output_is_redacted_in_state_and_report`); acceptance timeout kills the process group (`test_acceptance_timeout_kills_the_hung_grandchild_too`); huge acceptance output capped (`test_acceptance_huge_output_is_capped_and_keeps_the_end`); Windows-unsafe and `..` paths refused (`test_validate_rejects_*`); symlink escape detected (`test_symlink_pointing_outside_the_worktree_is_detected`); global agent cap (`test_global_agent_cap_bounds_concurrent_subprocesses`); budget reserve (`test_budget_reserve_keeps_parallel_steps_within_plan_budget`); test isolation from the real memory DB (`tests/test_isolation.py`).

**Fixed after this list was written** (each has a test, run with a real `claude` call for the `Popen` path): #1 approval is enforced (`plan.json` must match the approved hash, else the plan fails and is not respawned: `test_plan_edited_after_approval_is_refused`); #3 acceptance runs without `*KEY*/*TOKEN*/*SECRET*/*PASSW*/*AUTH*` env vars (`test_acceptance_command_gets_no_credentials_from_the_environment`); #7 planner trace is redacted before it is stored; #9 agent timeouts now kill the agent's whole process group (`test_agent_timeout_kills_the_agents_children_too`; a SIGKILLed runner still leaves orphans: Linux needs `PR_SET_PDEATHSIG` or a sandbox); #10b a `queued` step is reset on resume (`test_step_left_queued_by_a_killed_runner_is_run_again`); #12 gitleaks and gates have timeouts (`test_gate_tool_that_hangs_becomes_a_failed_gate_not_a_crash`). Also fixed: steps were marked failed when `.gitignore` listed `.pytest_cache` (`test_step_that_runs_pytest_is_committed_with_or_without_gitignore`); `flow-task retry <plan>` re-runs only the failed steps. Fixed in s0–s5 (each has a test, see the item): #2 sandbox (**PARTIAL**: claude on Linux with bwrap), #5 acceptance discipline, #6 secrets fail closed, #9 orphan agent groups (**PARTIAL**: Linux PDEATHSIG, plus `recover`), #10a `flow-task recover`, plus the cross-plan overlap warning and `flow-task clean` (see "Added" below). Still open: #4, #8, #11, #13, #14.

---

## P0 (critical)

### 1. Approval is not enforced: `plan_hash` is written but never checked
- **Risk:** the approved plan is not frozen. Anything that edits `plans/<pid>/plan.json` after `approve` changes what runs. `run()` reloads plan.json and runs it without comparing hashes. A resumed runner (after a crash) picks up the edited acceptance commands and steps. Sub-agents can reach that file: their worktree is `plans/<pid>/wt/<sid>`, so `../../plan.json` is one relative path away, and their tool allow-list (item 2) does not stop it.
- **Evidence:** `plan.py` `approve()` line 268 stores `plan_hash`; `plan.py` `run()` line 502 calls `load(pid)` and never compares the hash. `grep -n plan_hash flow_engine/*.py` shows only the write (268) and the display in `report()` (613). No test covers a changed plan.json after approval. **(by reading)**
- **Next action:** in `run()`, recompute the hash of plan.json and refuse to start (or resume) on mismatch, with a clear error. Add a test that edits plan.json after approve and expects the runner to refuse. Consider a copy of the approved plan kept outside `plans/<pid>/` that sub-agents cannot reach.

### 2. Sub-agents have no sandbox beyond a tool allow-list
- **Status: PARTIAL, FIXED for claude on Linux with bwrap (s1).** `sandbox.wrap()` runs `claude -p` under bwrap: read-only `/`, writable only the step worktree, `/tmp` and claude's own config (`test_wrap_binds_worktree_writable_root_read_only_and_not_plans`, `test_run_claude_wraps_command_when_bwrap_available`, `test_real_bwrap_allows_worktree_write_and_blocks_parent_write`). `flow-task doctor` shows the sandbox line. Residual: agy is not wrapped (its own sandbox mounts only `/mnt/second_drive` and `/tmp`); without bwrap (macOS, Windows, or blocked user namespaces) the only guard is the `escaping_paths` post-check, and a single warning is logged (`test_no_sandbox_logs_one_warning_and_fake_claude_still_runs`).
- **Risk:** write-capable sub-agents can change files outside their worktree and run arbitrary code. `Bash(python3:*)` allows any Python code, which can open any absolute path. `Edit` and `Write` have no path restriction. The worktree is a convention, not a boundary. A bad or prompt-injected step can touch the main repo, the user's home, or the memory DB.
- **Evidence:** `agents.py` `WRITE_TOOLS` line 30 and `BASH_ALLOW` line 31 (default includes `Bash(python3:*)`, `Bash(python:*)`); `agents.py` `run_claude` line 154–155 passes `--permission-mode acceptEdits`; `agents.py` `_run()` lines 141–144 sets only `cwd=o.workdir`. `plan.py` `do_step()` line 414 uses `WRITE_TOOLS`. Not run: whether `claude -p` with acceptEdits blocks writes outside the working directory. Agy `--mode accept-edits` has no equivalent check here. The sandbox that agy uses (only `/mnt/second_drive` and `/tmp` mounted, from `docs/evidence/README.md`) is outside this code.
- **Next action:** run sub-agents in a real sandbox (container or OS-level), or at least check `realpath` of every write after each step. Narrow `BASH_ALLOW` to the commands the acceptance needs. Add a probe test that a step cannot write outside its worktree.

### 3. Acceptance commands run with `shell=True`, from plan.json, with the full environment
- **Risk:** the acceptance command is a shell string written by the planner LLM and shown to the user only in `plan.md`. It runs on the host with the runner's full environment, so any API key in the environment is visible to it. `validate()` checks ids, paths and deps, but not the acceptance text.
- **Evidence:** `plan.py` `accept()` line 371 uses `shell=True` and does not pass `env=`, so it inherits `os.environ`. `plan.py` `validate()` lines 140–177 has no acceptance check. What was hardened: timeout with group kill, output to files, tail cap and redaction (`test_acceptance_timeout_kills_the_hung_grandchild_too`, `test_acceptance_huge_output_is_capped_and_keeps_the_end`). What remains: the command itself is not limited or reviewed beyond the plan review. The env leak was not run. **(by reading)**
- **Next action:** pass a reduced environment to acceptance (drop `*_KEY`, `*_TOKEN`, `*_SECRET` and similar). Require the user to approve each acceptance command in `plan.md` by hash (see item 1). Consider running acceptance in the same sandbox as item 2. Add a test that a planted env var is not visible to acceptance.

---

## P1 (high)

### 4. Sub-agents only see committed HEAD
- **Risk:** uncommitted work in the user's repo is invisible to the steps. Steps can duplicate or conflict with work in progress. The plan warns about it but does not stop it.
- **Evidence:** `plan.py` `do_step()` line 396 takes `base_sha` from the plan worktree's HEAD, and line 398 creates the step worktree from it. `create()` lines 214 and 233–235 record `dirty_files` and `render_md()` lines 182–184 print the warning. Test: `test_dirty_repo_plan_warns_and_runs_from_the_commit`.
- **Next action:** refuse to approve a plan when the repo is dirty (with an override flag), or let the user choose to commit first. Record the dirty list in the approval.

### 5. "Done" can be false: a step with no acceptance command passes on any change, and tests can be weakened
- **Status: FIXED (s2).** `validate()` rejects an empty or trivially-true acceptance command (`true`, `:`, `exit N`, `echo`/`printf` only) and names the step (`test_validate_rejects_trivial_acceptance_and_names_the_step`; `test_validate_accepts_real_acceptance` keeps real commands valid). The constraints gate fails a step that removes an assert (`test_constraints_fails_after_removing_one_assert`), deletes a test file (`test_constraints_fails_after_deleting_the_test_file`) or adds a skip/xfail/suppression marker (`test_constraints_fails_after_adding_a_skip_marker`); a refactor that keeps the assert count passes (`test_constraints_passes_a_refactor_that_keeps_the_assert_count`). Residual: acceptance still runs in the agent's own worktree, so the check is the diff gate, not a copy of the base tests.
- **Risk:** an empty `acceptance` skips the check entirely. A step that edits anything then counts as done. Acceptance runs in the same worktree the agent edits, so the agent can change the test file or `conftest.py` until the command passes. The constraints gate only greps the diff for `skip`, `xfail`, `noqa`, `@ts-ignore` and removed `def test_` lines. A weakened assertion is not caught.
- **Evidence:** `plan.py` `accept()` lines 366–368 returns `ok: True, skipped: True` for an empty command; `do_step()` line 449 fails only when there are no changes AND no acceptance. `gate.py` `run_gates()` lines 58–62 (regexes on the diff). `do_step()` line 424 runs acceptance in `wt`, the agent's tree. `integrate()` line 586 reruns the same commands on the merged tree, so it cannot catch this either. **(by reading)**
- **Next action:** reject steps whose acceptance is empty at validate time. Run acceptance from a copy of the base test files (or check that test files were not modified by the step). Add a gate that fails when an assert line is removed from a test.

### 6. Secrets: nothing scans what gets committed; the secret gate fails open
- **Status: FIXED (s3).** With gitleaks missing the gate runs the built-in regex scan (`secrets.py` `SECRET_PATTERNS`) over the step's added lines, and it fails on a hit without echoing the secret (`test_planted_key_in_untracked_file_fails_the_secrets_gate`, `test_key_in_added_line_of_a_tracked_file_fails_and_is_not_echoed`). Secret file names fail by name (`test_planted_env_file_fails_by_name`; `test_env_example_is_not_a_secret_file` keeps `.env.example` allowed). Clean code passes (`test_clean_file_passes_with_builtin_engine`). `flow-task doctor` reports gitleaks missing as a warning (`test_doctor_reports_missing_gitleaks_as_a_warning_that_does_not_fail`). Residual: the regex scan is shallower than gitleaks, see item 14.
- **Risk:** `redact()` only masks text (summaries, reports, recipes). It does not look at the code. `git add -A` commits any file the agent wrote, including `.env` or a key file. The `secrets` gate runs gitleaks only when it is installed; when it is missing the gate is reported as `ok` with `skipped`.
- **Evidence:** `plan.py` `do_step()` line 447 (`git add -A`) and `ADD` line 27 (excludes only caches). `gate.py` `run_gates()` lines 35–42 (gitleaks or `skipped` with `ok: True`). gitleaks is installed on this host (`/home/alexheiz/.local/bin/gitleaks`), so the gate runs here, but it was not exercised in this step. No test covers a committed secret file.
- **Next action:** make the secrets gate fail closed (or require gitleaks in `flow-task doctor`). Scan the step diff before commit, and refuse to commit known secret file names. Add a test with a planted `.env` in a step.

### 7. Secrets: some files are written before redaction
- **Risk:** the plan's goal, steps and planner trace are written to disk and to Redis without `redact()`. The planner trace contains agent error text, which can echo an API key.
- **Evidence:** `plan.py` `create()` line 240 (`plan.json`), line 241 (`state.json` with `plan_trace`, not redacted; `save()` at line 311 redacts only later writes), line 242 (`plan.md`), line 243 (`_mirror` sends `goal[:200]` to Redis). `report()` lines 653–654 do redact. **(by reading)**
- **Next action:** run `redact()` in `create()` on state, plan.md and the Redis mirror. Add a test where the planner error contains a key and check `state.json`, `plan.md` and `plans/` as a whole.

### 8. Cost is blind for agy: budget and per-step caps are not enforced
- **Risk:** `run_agy()` always returns cost 0.0 and never reads `o.budget`. Steps on agy spend nothing on the books, so `FLOW_PLAN_MAX_USD` and the per-step reserve only protect the claude and opencode backends. Pinned gpt-oss retries (up to 10 attempts, backoff cap 60 s) also run with no cost ceiling.
- **Evidence:** `agents.py` `run_agy()` line 206 (`return Result(True, ..., 0.0, ...)`, comment "agy reports tokens, not dollars"), lines 177–206 (no use of `o.budget`); `run_claude()` line 152 passes `--max-budget-usd`. `plan.py` `do_step()` line 414 gives each call `cap`, and `Ctx.free()` lines 330–332 counts only recorded cost. Test: `test_gpt_oss_parent_runs_the_whole_plan_on_that_model_only` (checks the model, not the cost).
- **Next action:** estimate agy cost from tokens with a per-model price table and enforce the cap by tokens as well as dollars. Add a test with a fake agy that reports tokens and check that the plan stops at the budget.

### 9. Killed runner leaves its sub-agents running (probe)
- **Status: PARTIAL, FIXED on Linux (s4).** Each agent starts in its own process group, and on Linux `PR_SET_PDEATHSIG` (set via ctypes in `agents._die_with_runner`) makes the kernel kill it when the runner dies: `test_agent_child_is_killed_when_its_runner_is_sigkilled`. The group ids are recorded in `state.json` (`agent_pgids`), and `flow-task recover` kills groups that are still alive after a dead runner: `test_orphan_agent_group_is_killed_by_recover`. Residual: macOS has no PDEATHSIG, so an orphan there lives until `recover` runs.
- **Risk:** the runner's sub-agents are not killed when the runner dies. They keep consuming tokens and writing into the worktree. On resume, the same step starts again while the old agent is still alive. The old agent's cost is never added to `state.json`.
- **Evidence:** **(probe)** a scratch script (not committed) ran `agents._run(["sleep", "40"], ...)` in a child started with `start_new_session=True`, then SIGKILLed that child. `sleep 40` survived as an orphan. Cause: `agents.py` `_run()` lines 141–144 uses plain `subprocess.run` with no process group, unlike `plan.py` `_new_group()` and `_kill_group()` (lines 339–355), which are used only for acceptance. The existing test `test_runner_killed_mid_step_is_resumed_by_the_listener_check` kills only the runner's processes and does not check for orphans.
- **Next action:** run agent subprocesses with `_new_group()` and kill the group on timeout and on runner exit (`atexit` / signal handler). Add a test that the agent child is gone after the runner is killed.

### 10. Recovery after a killed runner depends on the listener, and a `queued` step is never relaunched
- **Status: 10a FIXED (s4), 10b FIXED (earlier, `test_step_left_queued_by_a_killed_runner_is_run_again`).** `flow-task recover` finds plans in running/approved with a stale heartbeat and no live runner lock or pid, and resumes them without the listener (`test_dead_running_plan_is_resumed`, `test_approved_plan_with_no_runner_is_resumed_from_its_approval_time`, `test_cli_recover_prints_one_line_per_plan_acted_on`). A live runner lock or a fresh heartbeat is left alone (`test_plan_with_a_live_runner_lock_is_not_resumed`, `test_fresh_heartbeat_is_not_resumed`); a just-approved plan is left alone too (`test_approved_plan_approved_just_now_is_left_alone`).
- **Risk:** (a) a killed plan stays `running` until `resume_stale()` runs. That is called only by the listener's loop, so with no listener the plan is stuck. (b) `run()` resets only steps in `running`. A runner killed after a step is marked `queued` (line 547) and before its first save leaves it `queued`. `_launchable()` only launches `pending` steps, so the step never runs and the plan ends `partial` or `failed`.
- **Evidence:** `engine.py` line 203 is the only caller of `plan.resume_stale()`. `plan.py` `run()` lines 512–516 (reset `running` only), line 547 (`queued`), `_launchable()` lines 488–498 (requires `pending`). Test: `test_runner_killed_mid_step_is_resumed_by_the_listener_check` covers only the `running` case. The `queued` path is **(by reading)**, not reproduced.
- **Next action:** reset `queued` as well in `run()`. Add a `flow-task recover` command that does the stale check without the listener. Test a state.json with a `queued` step.

---

## P2 (medium)

### 11. Gates and acceptance run outside the global agent cap
- **Risk:** 12 concurrent plans can run 12 test suites at once, each with a pytest run of up to 600 s. The cap in `agents.py` limits only agent subprocesses.
- **Evidence:** `agents.py` `_slot()` is used only in `_run()` (line 143). `gate.py` `_run()` lines 18–22 and `plan.py` `accept()` line 371 do not use it.
- **Next action:** route gates and acceptance through the same slot pool (or a separate CPU pool).

### 12. A gitleaks run that hangs is not bounded
- **Risk:** `gate.run_gates()` calls gitleaks without a timeout. A hang blocks the step thread forever. (pytest in the same function has `timeout=600`, but a `TimeoutExpired` there is not caught by `_run()`, which handles only `OSError`, so it becomes a step crash.)
- **Evidence:** `gate.py` line 38 (no `timeout=`); `gate.py` line 50 (pytest timeout 600); `gate.py` `_run()` lines 18–22 (catches `OSError` only). **(by reading)**
- **Next action:** add a timeout to gitleaks and catch `TimeoutExpired` in `_run()`, returning a failed gate with a clear reason.

### 13. Only one merge-conflict rescue; step files are not enforced
- **Risk:** a second conflict fails the step even when the second run would have merged. Files outside the plan's `files` list only produce a warning in the report, so a step can change unrelated code.
- **Evidence:** `plan.py` `do_step()` lines 469–481 (one rescue, then `failed`); `do_step()` line 468 (`outside_plan` only shown in the report). Test: `test_merge_conflict_triggers_rescue_rerun` covers one rescue.
- **Next action:** turn `outside_plan` into a gate failure when the step is not allowed to touch those paths. Allow more than one rescue with a limit.

### 14. Redaction is a list of regexes
- **Risk:** secret shapes not in the list pass through. The generic `token|secret|password` rule needs a `key=value` shape. Redaction applies to some outputs only (item 7).
- **Evidence:** `plan.py` `SECRET_PATTERNS` lines 32–39; `test_redact_masks_each_secret_shape_and_keeps_the_words_around_it` (lists only the shapes it knows).
- **Next action:** add gitleaks-style rules, or treat redaction as best-effort and rely on item 6.

---

## Added (not on the original list)

- **Cross-plan overlap warning (s5).** At `approve`, a live plan on the same repo that lists the same files is reported as a warning; the approval is not blocked (`plan.overlaps()`). The warning is printed by `flow-task approve` (`test_cli_warns_on_overlap_before_the_approved_line`); the library call lists the overlaps (`test_approve_reports_only_live_plans_on_the_same_repo_that_share_files`). A plan with disjoint files gets no warning (`test_approve_disjoint_plan_has_no_overlap`). This is the warning only: merging two plans that touch the same file can still conflict.
- **`flow-task clean [--older-than DAYS] [--result-branches]` (s5).** Removes finished plans' step worktrees and `flowstep/*` branches. It keeps `flow/<pid>` result branches unless `--result-branches` is given (`test_clean_removes_worktrees_and_step_branches_and_keeps_result_branch`, `test_clean_removes_result_branch_only_when_asked`). It never touches a running plan (`test_clean_never_touches_live_plans`), skips a plan whose runner still holds the lock (`test_clean_skips_a_plan_whose_runner_still_holds_the_lock`), and `--older-than` filters on the finished time (`test_older_than_filters_by_finished_time`). CLI output: `test_clean_cli_prints_what_was_removed`.

## Reflection / anti-lost (s0)

Covered by `tests/test_reflection_journal.py`:
- Every attempt is written to `plans/<pid>/journal/<sid>.jsonl`, redacted, and a retry or resume is told the earlier attempts (`test_journal_keeps_every_attempt_redacted_and_brief_respects_the_limit`, `test_failed_attempt_is_journaled_and_the_retry_asks_for_a_reflection_first`, `test_resumed_step_is_told_its_earlier_attempts`).
- Loop detection: the same failure three times in a row ends the step as `stuck` without more attempts (`test_the_same_failure_three_times_ends_stuck_without_more_attempts`).
- Drift: a changed file outside the plan's `files` list is a warning, and its test file is not (`test_a_file_outside_the_plan_is_drift_but_its_test_file_is_not`). The merge still goes ahead.
- `flow-task where [<plan>]` and `STATUS.md` next to `state.json` name the next action in one short text, and stay under the size limit (`test_where_from_another_folder_names_the_next_action_and_writes_status_md`, `test_where_refuses_when_this_repo_has_no_plan`, `test_where_text_stays_under_the_limit_for_a_big_plan`).

## Not in scope of this step
- This step changes docs and the USAGE text only. The fixes above are in the step commits (s0–s5) and their tests.
- No real claude, agy or opencode run. The real-bwrap test (`test_real_bwrap_allows_worktree_write_and_blocks_parent_write`) checks nothing on hosts where bwrap does not work; it returns early instead of skipping, so a green run there does not prove the sandbox.
