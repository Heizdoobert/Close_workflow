"""doctor lines for the mode split: which CLI backends and which API keys are usable right now.

Prints presence only ("present" / "missing"), never a key value or a key prefix.
"""

import os
import shutil

from flow_engine import agents, api

CLI_BINS = {
    "claude": agents.claude_bin,
    "agy": lambda: os.environ.get("FLOW_AGY") or shutil.which("agy"),
    "opencode": lambda: os.environ.get("FLOW_OPENCODE") or shutil.which("opencode"),
}


def report():
    """One line per mode: CLI backends (binary found) and API providers (key set in the environment)."""
    lines = []
    for name, locate in CLI_BINS.items():
        lines.append(f"mode cli {name}: {'present' if locate() else 'missing'}")
    for provider, env in api.KEY_ENV.items():
        lines.append(f"mode api {provider} ({env}): {'present' if os.environ.get(env) else 'missing'}")
    return lines
