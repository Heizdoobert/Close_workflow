# FLOW_SKILL: closed-loop task engine

Drop a task, a cheap Claude worker runs it, state goes to Redis, logs go to `logs/flow_engine.log` (JSON lines), and [Agent_system](../Agent_system) gives it memory. Linux, macOS, Windows. **Install: [INSTALL.md](INSTALL.md).** Agent? Read [AGENT_SETUP.md](AGENT_SETUP.md): it installs and wires everything with no manual step.

```
flow-task enqueue "task text"  ->  Redis task:<id>  ->  listener  ->  claude -p (haiku, read-only, ~7k tokens)
                                                                          -> gates (FLOW_GATE=1) -> retry with gate output
                                                                          -> taskstate:<id>, logs/flow_engine.log
```

## Layout
- `flow_engine/` stdlib Python core: `resp.py` (Redis client), `engine.py` (tasks, runner, listener), `gate.py` (0-token gates), `bridge.py` (Agent_system recall/learn), `cli.py` (`flow-task`).
- `install.py` + `install.sh` / `install.ps1` cross-OS installer. `run_task.sh`, `listener.sh` are shims kept for existing `/usr/local/bin/flow-task` and units.
- `docker-compose.yml` Redis (password from `.env`).
- `tests/` pytest with a fake Redis: `pip install -e . pytest && pytest`. `flow-task selftest` runs the real plan flow once in a throwaway repo (about 0.03 USD).

## Use
```bash
flow-task doctor
flow-task enqueue "summarize README.md"      # or: echo "task:[...]" > incoming_tasks/t.txt
flow-task status <id>                        # status, cost, tokens, result
```
Task ids and state: Redis `task:<id>` (queued payload, 30-day TTL) and `taskstate:<id>` (status running/done/failed, cost, tokens, attempts, result). Failed files go to `incoming_tasks/failed/`.

## Logs
`logs/flow_engine.log`, one JSON object per line. Cost per task: `grep '"event": "exec"' logs/flow_engine.log | jq .cost_usd`. A plan's state: `flow-task where <plan>`.

Settings and the Agent_system wiring are in [INSTALL.md](INSTALL.md).
