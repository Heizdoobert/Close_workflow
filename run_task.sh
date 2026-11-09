#!/usr/bin/env bash
# Wrapper to trigger the shared_state workflow.
# Usage: ./run_task.sh "tôi có task cho bạn:[task description]"

if [[ -z "$1" ]]; then
  echo "Error: No task message provided."
  echo "Usage: $0 \"tôi có task cho bạn:[...task...]\""
  exit 1
fi

MESSAGE="$1"

# Call the orchestrator Python module, passing the task message.
# python vs python3: distro-dependent, pick whichever exists
if command -v python >/dev/null 2>&1; then PY=python; else PY=python3; fi
"$PY" "$(dirname "$0")/../shared_state.py" "$MESSAGE"
