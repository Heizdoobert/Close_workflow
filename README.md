# Closed‑Loop Task Engine

This folder contains the minimal set of files required to run the **closed‑loop workflow** with Claude, Redis persistence, and optional central logging (Loki + Grafana).

## Files included
- `listener.sh` – daemon that watches `incoming_tasks/` and logs detailed output.
- `run_task.sh` – wrapper that invokes the `closed_loop_task_engine` sub‑agent.
- `install.sh` – script that spins up Redis and the Loki/Grafana stack, defines the sub‑agent, and starts the listener.
- `docker-compose.yml` – starts a Redis container.
- `docker-compose-logging.yml` – starts Loki, Promtail, and Grafana containers (local, free).
- `loki/config/local-config.yaml` – Loki configuration.
- `promtail/config/promtail.yaml` – Promtail configuration to ship logs from `logs/*.log` to Loki.
- `closed_loop_task_engine.json` – sub‑agent definition (Claude model, Redis persistence, optional Arize Phoenix logging).

## Usage
1. Make scripts executable:
   ```bash
   chmod +x install.sh listener.sh run_task.sh
   ```
2. Run the installer (it will start Redis, Loki/Grafana, define the sub‑agent and launch the listener):
   ```bash
   ./install.sh
   ```
3. Add a task by creating a `.txt` file in `incoming_tasks/`:
   ```bash
   echo "tôi có task cho bạn:[Xây API quản lý sản phẩm]" > incoming_tasks/task1.txt
   ```
   The listener will automatically process the task, store state in Redis, and push logs to Loki (viewable in Grafana at `http://localhost:3000`).

Feel free to edit the scripts or configuration files to adapt to your environment.
