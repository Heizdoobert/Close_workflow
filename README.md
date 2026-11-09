# FLOW_SKILL: closed-loop task engine

Drop a task, a cheap Claude worker runs it, state goes to Redis, logs go to Loki/Grafana, and [Agent_system](../Agent_system) gives it memory. Linux, macOS, Windows. **Install: [INSTALL.md](INSTALL.md).** Agent? Read [AGENT_SETUP.md](AGENT_SETUP.md): it installs and wires everything with no manual step.

```
flow-task enqueue "task text"  ->  Redis task:<id>  ->  listener  ->  claude -p (haiku, read-only, ~7k tokens)
                                                                          -> gates (FLOW_GATE=1) -> retry with gate output
                                                                          -> taskstate:<id>, logs/flow_engine.log -> Promtail -> Loki -> Grafana
```

## Layout
- `flow_engine/` stdlib Python core: `resp.py` (Redis client), `engine.py` (tasks, runner, listener), `gate.py` (0-token gates), `bridge.py` (Agent_system recall/learn), `cli.py` (`flow-task`).
- `install.py` + `install.sh` / `install.ps1` cross-OS installer. `run_task.sh`, `listener.sh` are shims kept for existing `/usr/local/bin/flow-task` and units.
- `docker-compose.yml` Redis (password from `.env`), `docker-compose-logging.yml` Loki + Promtail + Grafana, `grafana/` dashboards.
- `tests/` pytest with a fake Redis: `pip install -e . pytest && pytest`.

## Use
```bash
flow-task doctor
flow-task enqueue "summarize README.md"      # or: echo "task:[...]" > incoming_tasks/t.txt
flow-task status <id>                        # status, cost, tokens, result
```
Task ids and state: Redis `task:<id>` (queued payload, 30-day TTL) and `taskstate:<id>` (status running/done/failed, cost, tokens, attempts, result). Failed files go to `incoming_tasks/failed/`.

## Grafana
`http://localhost:3000` (default login admin/admin: change it), dashboard "Flow Task Engine". LogQL: `{job="flow"}`, cost per task: `{job="flow"} |= "\"event\": \"exec\"" | json | unwrap cost_usd`.

Settings and the Agent_system wiring are in [INSTALL.md](INSTALL.md).
