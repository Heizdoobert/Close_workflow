#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""listener.py – Cross-platform daemon for Close_workflow (Windows & Linux).
Monitors incoming_tasks/ directory, processes tasks with shared_state DAG,
writes logs to logs/, and archives finished tasks to incoming_tasks/processed/.
Robust against different Windows encodings (UTF-8, UTF-8-BOM, UTF-16, ANSI).
"""

import os
import sys
import time
import shutil
import datetime
from pathlib import Path

# Redirected stdout/stderr default to cp1252 on Windows – emoji in prints
# would crash the daemon (same fix as close_workflow_cli.py).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Setup paths
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from shared_state import orchestrate

WATCH_DIR = SCRIPT_DIR / "incoming_tasks"
PROCESSED_DIR = WATCH_DIR / "processed"
LOG_DIR = SCRIPT_DIR / "logs"


def ensure_dirs():
    WATCH_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def read_file_content(file_path: Path) -> str:
    """Read file attempting common Windows and Linux text encodings."""
    encodings = ["utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"]
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as f:
                content = f.read().strip()
                if content:
                    return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Fallback to binary decode with replace
    with open(file_path, "rb") as f:
        return f.read().decode("utf-8", errors="replace").strip()


def process_file(file_path: Path):
    if not file_path.is_file() or file_path.suffix.lower() != ".txt":
        return

    message = read_file_content(file_path)
    if not message:
        return

    print(f"🚀 Detected task from {file_path.name}: {message}", flush=True)
    timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"{file_path.stem}_{timestamp_str}.log"

    # Execute workflow DAG
    try:
        final_state = orchestrate(task_msg=message)
        import json
        log_content = json.dumps({"data": final_state.data, "history": final_state.history}, ensure_ascii=False, indent=2)
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(log_content + "\n")
        print(f"📝 Output written to {log_file}", flush=True)
    except Exception as e:
        print(f"❌ Execution failed: {e}", flush=True)
        with open(log_file, "w", encoding="utf-8") as f:
            f.write(f"ERROR: {str(e)}\n")

    # Move to processed
    target_path = PROCESSED_DIR / file_path.name
    if target_path.exists():
        target_path = PROCESSED_DIR / f"{file_path.stem}_{timestamp_str}.txt"
    try:
        shutil.move(str(file_path), str(target_path))
        print(f"✅ Moved task to {target_path.name}", flush=True)
    except Exception as e:
        print(f"⚠️ Could not move file: {e}", flush=True)


def run_daemon(poll_interval: float = 1.0):
    ensure_dirs()
    print(f"🔔 Close_workflow Listener is active!", flush=True)
    print(f"📂 Watching: {WATCH_DIR}", flush=True)
    print(f"📝 Logging:  {LOG_DIR}", flush=True)

    try:
        while True:
            for item in WATCH_DIR.glob("*.txt"):
                if item.is_file():
                    process_file(item)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n🛑 Listener stopped gracefully.", flush=True)


if __name__ == "__main__":
    run_daemon()
