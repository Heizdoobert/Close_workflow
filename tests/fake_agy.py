#!/usr/bin/env python3
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakelib

a = sys.argv[1:]
model = a[a.index("--model") + 1] if "--model" in a else "NONE"
conv = a[a.index("--conversation") + 1] if "--conversation" in a else ""
prompt = fakelib.resolve(a[a.index("-p") + 1])
fakelib.log("agy", model, conv or "-")


def out(status, response, code=0):
    print(json.dumps({"conversation_id": "conv-1", "status": status, "response": response, "usage": {"input_tokens": 7, "output_tokens": 3}}))
    sys.exit(code)


if model in os.environ.get("FAKE_AGY_DEAD", "").split(","):
    out("ERROR", "401 invalid api key", 1)
if fakelib.next_count("agy_count") < int(os.environ.get("FAKE_AGY_503_FIRST", "0")):
    out("ERROR", "503 model overloaded, capacity limit", 1)
if fakelib.next_count("agy_flaky") < int(os.environ.get("FAKE_AGY_FLAKY_FIRST", "0")):
    out("ERROR", "something odd happened", 1)
out("SUCCESS", fakelib.work(prompt))
