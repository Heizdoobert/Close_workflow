# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `LICENSE` (MIT), `SECURITY.md`, `CHANGELOG.md` and `CONTRIBUTING.md`.
- `tests/test_repo_files.py` checks that these files exist and carry their required content.

### Changed

- Claims layer (anti-lying) before SHIP: step PASS/FAIL claims rerun at the step commit, `analysis.md` hash checked, one LLM cross-check; an unrun check is NO-GO (`FLOW_CLAIMS`, `FLOW_CLAIMS_USD`).
- `pyproject.toml` declares the MIT license.

### Removed

- Loki, Promtail and Grafana (`docker-compose-logging.yml`, `loki/`, `promtail/`, `grafana/`, `install.py --no-central-log`). Logs stay in `logs/flow_engine.log` as JSON lines.
- `legacy/` and its layout test. Git history keeps the files.
- CI matrix cut from 7 legs to 3 (ubuntu 3.12, windows 3.12, ubuntu-22.04 3.9); macOS and 3.10 legs dropped. Dependabot no longer watches pip (no runtime dependencies).

## [0.5.0]

### Added

- Closed-loop task engine: Redis queue, `claude -p` worker, scripted gates and Loki/Grafana logs.
- Agentic plan flow with parallel sub-agents in git worktrees, reports and reflection journal.
- `flow-task selftest` and the adoption ladder in `AGENT_SETUP.md`.
