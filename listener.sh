#!/usr/bin/env bash
# Compatibility shim: the listener is `flow-task listen` (cross-platform polling, single instance via file lock).
exec "$(dirname "$0")/run_task.sh" listen
