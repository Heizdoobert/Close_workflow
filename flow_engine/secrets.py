"""Secret shapes shared by the redaction of stored text and the secrets gate (stdlib only, no imports from the engine)."""

import fnmatch
import os
import re

# likely secrets; masked before anything is written to state, reports or recipes (the goal in plan.json is user input, left as is)
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{12,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-]{8,}=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret[_-]?key|token|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{6,}"), r"\1[REDACTED]"),
]
# one name per SECRET_PATTERNS entry, shown in gate findings
RULE_NAMES = ["private-key", "sk-key", "aws-key", "github-token", "bearer-token", "assigned-secret"]
SHAPE_RULES = {"private-key", "sk-key", "aws-key", "github-token"}
# file names that look like secrets; .env.example and friends are templates, not secrets
SECRET_FILE_GLOBS = [".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519", "credentials.json"]
SECRET_FILE_SAFE = [".env.example", ".env.sample", ".env.template"]


def redact(text):
    """Mask likely secrets (keys, tokens, passwords, private key blocks) in text; the surrounding words stay."""
    text = "" if text is None else str(text)
    for rx, repl in SECRET_PATTERNS:
        text = rx.sub(repl, text)
    return text


def redact_shapes(text):
    """Mask only the secrets with a fixed shape (private key block, sk-, AKIA, gh tokens). For code a model must read: the assigned-secret rule would also rewrite `token = self.next_token()`."""
    text = "" if text is None else str(text)
    for (rx, repl), name in zip(SECRET_PATTERNS, RULE_NAMES):
        if name in SHAPE_RULES:
            text = rx.sub(repl, text)
    return text


def redact_obj(o):
    """redact() applied to every string inside a JSON-able value. Never redact the serialised text instead: a mask that swallows a closing quote or brace leaves a file that no longer parses."""
    if isinstance(o, dict):
        return {redact_obj(k) if isinstance(k, str) else k: redact_obj(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [redact_obj(v) for v in o]
    return redact(o) if isinstance(o, str) else o


def scan_line(line):
    """Names of the secret rules that match one line of text. Never returns the matched text."""
    return [name for (rx, _), name in zip(SECRET_PATTERNS, RULE_NAMES) if rx.search(line)]


def secret_file_name(path):
    """True when the file name (not its content) looks like a secret file."""
    base = os.path.basename(path)
    if base in SECRET_FILE_SAFE:
        return False
    return any(fnmatch.fnmatchcase(base, g) for g in SECRET_FILE_GLOBS)
