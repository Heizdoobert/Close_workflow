#!/usr/bin/env bash
# Compatibility shim (systemd units, /usr/local/bin/flow-task): the engine is flow_engine/ (stdlib Python, all OS).
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
# -c, not -m: -m puts the caller's cwd first on sys.path, so a flow-task run from another checkout or a plan worktree loaded that folder's engine (and its queue and .env)
exec "$PY" -c 'import runpy, sys; sys.path[0] = sys.argv.pop(1); runpy.run_module("flow_engine", run_name="__main__", alter_sys=True)' "$DIR" "$@"
