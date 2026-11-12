"""A whole plan with the API backend in the chain: steps fall to the CLI (the API refuses them), review and the SHIP personas are served by a fake OpenAI server.

Asserts the token ledger adds up, the API rows carry the fake server's usage, and a key-shaped canary that the fake server echoes
reaches no file under the plan directory. Fake binaries and helpers come from test_plan.py; the fake server is tests/fake_api.py. No real key, no network beyond 127.0.0.1.
"""

import json

import pytest

import fake_api
from flow_engine import api, method, plan
from test_plan import env, step

API_SPEC = "api:openai/example-model"
DUMMY_KEY = "dummy-openai-key-not-real"
# built at runtime with join (not +, which CPython folds into one constant in the .pyc) so neither this source nor its bytecode holds a key-shaped literal
CANARY = "".join(["sk-", "test", "canary", "0123456789abcdef"])
FILES = ["a.py", "b.py", "c.py"]  # three files: a change above 2 files means the SHIP personas run


@pytest.fixture
def server(monkeypatch):
    reply = {**fake_api.OPENAI_REPLY, "choices": [{"message": {"role": "assistant", "content": f"plain prose, not JSON, key {CANARY} here"}}]}
    monkeypatch.setitem(fake_api.PATHS, "/v1/chat/completions", reply)  # the usage numbers stay the fake server's own
    srv = fake_api.start()
    monkeypatch.setenv("FLOW_API_BASE_URL", srv.base)
    monkeypatch.setenv("OPENAI_API_KEY", DUMMY_KEY)
    monkeypatch.setenv("FLOW_PRICE_OPENAI_EXAMPLE_MODEL", "1.0,0.1,1.25,5.0")
    monkeypatch.setenv("FLOW_BACKENDS", f"{API_SPEC},claude:haiku")
    monkeypatch.setenv("FLOW_PLAN_MAX_TOKENS", "10000000")  # far above the run: the budget must not bite
    monkeypatch.delenv("FLOW_API_CACHE", raising=False)
    api.breaker.clear()
    api.breaker_at.clear()
    api.cache.clear()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_whole_plan_with_api_in_the_chain_counts_tokens_and_never_leaks_the_key(env, server):
    env.script([step("s1", files=FILES, acc="test -f a.py && test -f b.py && test -f c.py")],
               {"s1": {"files": {f: f"{f} = 1\n" for f in FILES}}})
    pid = plan.create("g")
    assert plan.approve(pid, wait_for=True) == "done"

    d = env.home / "plans" / pid
    st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    assert st["status"] == "done"
    calls = st["calls"]

    # the planner and the step use the same chain: the API refuses calls that are not prompt-only, so the CLI answers them
    assert [(c["role"], c["backend"]) for c in calls if c["role"] in ("planner", "step:s1")] == [("planner", "claude:haiku"), ("step:s1", "claude:haiku")]
    assert all(c["backend"] == "claude:haiku" for c in calls if c["role"].startswith("step:"))

    # review and each SHIP persona were served by the API backend
    served = [c for c in calls if c["role"] == "review" or c["role"].startswith("ship:")]
    assert {c["role"] for c in served} >= {"review"} | {f"ship:{n}" for n in method.PERSONAS}
    assert all(c["backend"] == API_SPEC for c in served)
    # the fake server's usage: 20 prompt tokens of which 8 cached, 6 completion tokens
    for c in served:
        assert (c["input_tokens"], c["cache_read"], c["output_tokens"]) == (12, 8, 6), c

    # the totals are the sum of the ledger rows, and the API rows are in them
    assert st["tokens"]
    for k, v in st["tokens"].items():
        assert v == sum(c.get(k, 0) for c in calls), k
    assert st["tokens"]["cache_read"] >= 8 * len(served)

    rep = (d / "report.md").read_text(encoding="utf-8")
    assert "## Token tổng" in rep

    # the canary the API echoed reaches no file under the plan directory
    files = [p for p in d.rglob("*") if p.is_file()]
    assert (d / "journal").is_dir() and any((d / "journal").iterdir())
    assert any(p.name == "state.json" for p in files) and any(p.name == "report.md" for p in files)
    for p in files:
        data = p.read_bytes()
        assert CANARY.encode() not in data, p
        assert DUMMY_KEY.encode() not in data, p
    assert "[REDACTED]" in json.dumps(st.get("review", {}))  # the reply tail was seen, and redacted
    assert server.last_path == "/v1/chat/completions"
