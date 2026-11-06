#!/usr/bin/env bash
# Wrapper to trigger the closed-loop task engine sub-agent.
# Usage: ./run_task.sh "tôi có task cho bạn:[your task description]"

if [[ -z "$1" ]]; then
  echo "Error: No task message provided."
  echo "Usage: $0 \"tôi có task cho bạn:[...task...]\""
  exit 1
fi

MESSAGE="$1"

# Invoke the sub‑agent defined as "closed_loop_task_engine".
# The Antigravity CLI provides the `agy subagent invoke` command.
# Adjust the command path if your CLI is installed elsewhere.

agy subagent invoke \
  --name closed_loop_task_engine \
  --prompt "$MESSAGE" \
  --role "Closed‑Loop Task Orchestrator" \
  --workspace inherit

# The sub‑agent will run asynchronously and will notify this session when finished.
