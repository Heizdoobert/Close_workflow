#!/bin/bash
# reward: bench analyze --json prints the documented JSON line, then the NEXT line
set -euo pipefail
repo="$PWD"
work="$(mktemp -d)"
mkdir -p "$work/harness/runs/1/trace"
cat > "$work/harness/runs/1/results.json" <<'JSON'
{"run": 1, "complete": true, "tasks": {"t1": {"reward": 0}, "t2": {"reward": 1}}}
JSON
cat > "$work/harness/runs/1/trace/t1.jsonl" <<'JSON'
{"kind": "tool", "turn": 3, "tool": "Edit", "file": "flow_engine/gate.py"}
{"kind": "accept", "exit": 1, "out": ""}
JSON
cd "$work"
out="$(PYTHONPATH="$repo" python3 -m flow_engine bench analyze --json)"
OUT="$out" python3 - <<'PY'
import json, os
lines = os.environ["OUT"].strip().splitlines()
assert json.loads(lines[0]) == {"run": 1, "failing": {"t1": {"class": "gates", "turn": 3}}}, lines
assert lines[-1].startswith("NEXT: "), lines
PY
