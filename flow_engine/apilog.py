"""One JSON line per API attempt, written to the flow log (job="flow" in Loki) so Grafana can chart API calls.

The line carries counts, cost, latency and status only: no prompt, no answer and no key. Its fields are fixed by line().
"""

import datetime
import json

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read", "cache_create")


def line(role, backend, model, usage, cost, latency_ms, status):
    """Single-line JSON. cost None means the price is unknown: it is written as null, never as 0."""
    counts = {k: int((usage or {}).get(k) or 0) for k in TOKEN_KEYS}
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "api",
        "role": str(role or ""),
        "backend": str(backend),
        "model": str(model),
        **counts,
        "cost_usd": None if cost is None else round(float(cost), 10),  # a small call is 1e-5 USD: keep the digits
        "duration_ms": int(latency_ms),
        "status": str(status),
    }
    return json.dumps(rec, ensure_ascii=False)
