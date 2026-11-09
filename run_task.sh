#!/usr/bin/env bash
# Compatibility shim (systemd units, /usr/local/bin/flow-task): the engine is flow_engine/ (stdlib Python, all OS).
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
exec env PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" "$PY" -m flow_engine "$@"
