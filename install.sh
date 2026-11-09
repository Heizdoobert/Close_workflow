#!/usr/bin/env bash
# Linux/macOS bootstrap: finds Python 3.9+ and hands over to install.py (all logic lives there).
cd "$(dirname "$0")" || exit 1
for p in python3.12 python3.11 python3.10 python3 python; do
  command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' && exec "$p" install.py "$@"
done
echo "Python 3.9+ not found. Install it (apt install python3-venv / brew install python) and re-run." >&2
exit 1
