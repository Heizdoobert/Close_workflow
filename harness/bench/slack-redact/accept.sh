#!/bin/bash
# reward: redact() and redact_shapes() mask a Slack-shaped token, RULE_NAMES stays aligned
set -euo pipefail
python3 - <<'PY'
from flow_engine import secrets
token = "xox" + "b-" + "1234567890-abcdefghij"  # built here so scanners do not flag this file
text = f"a {token} b"
assert secrets.redact(text) == "a [REDACTED] b", secrets.redact(text)
assert secrets.redact_shapes(text) == "a [REDACTED] b", secrets.redact_shapes(text)
assert "slack-token" in secrets.RULE_NAMES and "slack-token" in secrets.SHAPE_RULES
assert len(secrets.RULE_NAMES) == len(secrets.SECRET_PATTERNS)
assert secrets.redact("xoxb-short") == "xoxb-short"  # under 10 characters after the dash: not a token
assert secrets.redact("sk-" + "a" * 20) == "[REDACTED]"  # the old rules still work
PY
