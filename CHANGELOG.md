# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `LICENSE` (MIT), `SECURITY.md`, `CHANGELOG.md` and `CONTRIBUTING.md`.
- `tests/test_repo_files.py` checks that these files exist and carry their required content.
- `flow-task bench run`, `compare`, `analyze` and `change`: stream-json tool events in `run_claude`, a manifest with 4 fields, and rollback on regression.
- `flow-task bench`: Bash tool events carry `cmd` (the first 120 characters of the command).

### Changed

- Claims layer (anti-lying) before SHIP: step PASS/FAIL claims rerun at the step commit, `analysis.md` hash checked, one LLM cross-check; an unrun check is NO-GO (`FLOW_CLAIMS`, `FLOW_CLAIMS_USD`).
- `pyproject.toml` declares the MIT license.

### Security

- Bench needs the bwrap sandbox (`FLOW_BENCH_UNSANDBOXED=1` opts out) and runs `accept.sh` with a scrubbed environment.
- Bench: corrupt results, trace or manifest files and symlinked component paths now give `bench error` instead of a traceback or an outside-repo read.

### Removed

- Loki, Promtail and Grafana (`docker-compose-logging.yml`, `loki/`, `promtail/`, `grafana/`, `install.py --no-central-log`). Logs stay in `logs/flow_engine.log` as JSON lines.
- `legacy/` and its layout test. Git history keeps the files.
- CI matrix cut from 7 legs to 3 (ubuntu 3.12, windows 3.12, ubuntu-22.04 3.9); macOS and 3.10 legs dropped. Dependabot no longer watches pip (no runtime dependencies).

## [0.5.0]

### Added

- Closed-loop task engine: Redis queue, `claude -p` worker, scripted gates and Loki/Grafana logs.
- Agentic plan flow with parallel sub-agents in git worktrees, reports and reflection journal.
- `flow-task selftest` and the adoption ladder in `AGENT_SETUP.md`.
