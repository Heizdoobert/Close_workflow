#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakelib

model = sys.argv[sys.argv.index("--model") + 1]
prompt = sys.stdin.read()
fakelib.log("claude", model)


def out(result, err=False, code=0):
    print(json.dumps({"type": "result", "is_error": err, "result": result, "total_cost_usd": 0.001, "usage": {"input_tokens": 10, "output_tokens": 5}}))
    sys.exit(code)


if fakelib.next_count("claude_count") < int(os.environ.get("FAKE_503_FIRST", "0")):
    out("API Error: 503 Service Unavailable: model overloaded", True, 1)
if model == os.environ.get("FAKE_BAD_MODEL"):
    out("invalid request: this model rejects the call", True, 1)
out(fakelib.work(prompt))
