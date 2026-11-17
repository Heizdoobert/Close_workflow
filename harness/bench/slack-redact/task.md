Make the secret redaction in flow_engine/secrets.py mask Slack tokens.

A Slack token is "xox" followed by one of the letters b, a, p, r or s, a "-", and then at least 10 characters from [A-Za-z0-9-].
- Add one entry for it to SECRET_PATTERNS that replaces the whole token with "[REDACTED]".
- Add the matching name "slack-token" to RULE_NAMES at the same position (RULE_NAMES must stay aligned with SECRET_PATTERNS, one name per pattern).
- Add "slack-token" to SHAPE_RULES, so redact_shapes() masks it too.
Do not change the other patterns and do not edit other files.
