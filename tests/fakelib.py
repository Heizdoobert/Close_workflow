"""Shared behaviour of the fake claude/agy binaries. Driven entirely by env vars so each test scripts its own scenario."""
import fcntl
import json
import os
import re
import sys
import time

D = os.environ["FAKE_STATE_DIR"]


ANALYSIS = {"research": [{"file": "README.md", "finding": "the repo only holds a readme"}], "root_cause": "nothing implements the goal yet",
            "approach": "add the files the steps list, one module per step", "alternatives": [{"option": "one big file", "rejected_because": "steps could not run in parallel"}],
            "fix": "small modules, each checked by its own acceptance command", "blast_radius": {"affected": ["README.md"], "risks": "none: only new files are added"},
            "spec": "each step writes its file; the acceptance command imports it and checks the value"}


def log(*f):
    with open(os.path.join(D, "calls.log"), "a") as fh:
        fh.write("\t".join(str(x) for x in f) + "\n")


def next_count(name):
    p = os.path.join(D, name)
    with open(p, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        n = int(fh.read() or 0)
        fh.seek(0)
        fh.truncate()
        fh.write(str(n + 1))
    return n


def resolve(prompt):
    """A resumed conversation remembers its task: the 'continue' nudge maps back to the stored prompt."""
    keep = os.path.join(D, "last_prompt")
    if "interrupted by an error" in prompt and os.path.exists(keep):
        return open(keep).read()
    open(keep, "w").write(prompt)
    return prompt


def work(prompt):
    """Return the text the 'model' answers; performs the file edits a step asks for in the cwd."""
    if "planning agent" in prompt:
        text = open(os.environ["FAKE_PLAN"]).read()
        try:  # a scripted plan without an analysis block gets a valid one; FAKE_NO_ANALYSIS=1 leaves it out
            plan = json.loads(text)
            if "analysis" not in plan and not os.environ.get("FAKE_NO_ANALYSIS"):
                plan["analysis"] = ANALYSIS
            return json.dumps(plan)
        except ValueError:
            return text
    if "review and simplification agent" in prompt:
        log("review")
        return os.environ.get("FAKE_REVIEW", '{"review":[],"simplify":[],"edge_cases":["empty input"]}')
    m = re.search(r"Your step \[([\w-]+)\]", prompt)
    sid = m.group(1)
    spec = json.load(open(os.environ["FAKE_STEPS"])).get(sid, {})
    log("step-start", sid, time.time())
    time.sleep(spec.get("sleep", 0))
    first_try = "did not pass the automated checks" not in prompt
    files = spec.get("files", {}) if first_try or "fixed_files" not in spec else spec["fixed_files"]
    for path, content in files.items():
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if "Other steps were merged" in prompt and os.path.exists(path):
            content = open(path).read() + content  # rescue run builds on the merged tree
        open(path, "w").write(content)
    log("step-end", sid, time.time())
    return f"step {sid} done: wrote {', '.join(files) or 'nothing'}"

