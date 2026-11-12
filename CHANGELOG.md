# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `LICENSE` (MIT), `SECURITY.md`, `CHANGELOG.md` and `CONTRIBUTING.md`.
- `tests/test_repo_files.py` checks that these files exist and carry their required content.

### Changed

- `pyproject.toml` declares the MIT license.

## [0.5.0]

### Added

- Closed-loop task engine: Redis queue, `claude -p` worker, scripted gates and Loki/Grafana logs.
- Agentic plan flow with parallel sub-agents in git worktrees, reports and reflection journal.
- `flow-task selftest` and the adoption ladder in `AGENT_SETUP.md`.
