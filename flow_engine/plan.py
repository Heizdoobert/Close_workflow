"""plan -> human approval -> sub-agents in isolated git worktrees (parallel where independent) -> evidence report.

Layout, per plan: <ROOT>/plans/<pid>/{plan.json, plan.md, state.json, STATUS.md, run.log, run.lock, report.md, report.json, journal/, wt/}
Git: branch flow/<pid> holds the merged result; each step works on flowstep/<pid>/<step> in its own worktree and is merged
only after its gates and acceptance command pass. Nothing touches your current branch: you review and merge flow/<pid>.
state.json is the source of truth (Redis is a best-effort mirror), so a crashed runner can be resumed.
journal/<sid>.jsonl keeps one line per attempt, so a retried or resumed step knows what it already tried.
"""

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from flow_engine import agents, bridge, engine, gate, journal

ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
IDENT = ["-c", "user.name=flow-engine", "-c", "user.email=flow@localhost"]
JUNK = [":(glob)**/__pycache__/**", ":(glob)**/*.pyc", ":(glob).pytest_cache/**"]  # build junk a repo without a .gitignore would otherwise commit
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
WIN_BAD_CHARS = re.compile(r'[<>:"|?*]')
SECRET_ENV = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH)")
ACCEPT_TAIL_BYTES = 64 * 1024  # per stream; only the last part is kept for the report and the retry prompt
# likely secrets; masked before anything is written to state, reports or recipes (the goal in plan.json is user input, left as is)
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{12,}"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/\-]{8,}=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?key|secret[_-]?key|token|secret|password|passwd)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{6,}"), r"\1[REDACTED]"),
]


class PlanError(Exception):
    pass


PLAN_PROMPT = """You are the planning agent of an automated engineering system. Explore the repository read-only, then reply with ONLY one JSON object, no prose, no code fences:
{{"steps":[{{"id":"s1","title":"short imperative","reason":"why this step is needed for the goal","deps":[],"files":["relative/paths/this/step/creates/or/edits"],"acceptance":"ONE shell command that exits 0 only if this step is really done (e.g. python3 -m pytest -q tests/test_x.py; always `python3 -m pytest`, never a bare `pytest`), run from the repo root","prompt":"self-contained instructions for the engineer doing this step"}}]}}
Rules: at most {max} steps. Steps that do not depend on each other run in parallel in separate git worktrees, so give them disjoint files. "deps" lists step ids that must finish first. The acceptance command must verify behaviour, not just that a file exists. Stay inside the repo.
{ctx}Goal: {goal}"""

STEP_PROMPT = """You are a sub-agent doing ONE step of a larger plan, inside an isolated git worktree (your current directory). Do only this step.
Overall goal: {goal}
Your step [{id}] {title}
Why: {reason}
Files you are expected to touch: {files}
Instructions: {prompt}
{deps}Acceptance check (it will be run for you afterwards): {acceptance}
When finished, verify your own work, then reply in 2-4 sentences: what you changed and what the check showed. Do not commit. Do not touch anything outside this directory."""
EARLIER = "Earlier attempts at this step (do not repeat what failed):"
REFLECT = "First, open your reply with one line 'REFLECTION: <what failed>, <why>, <what I change>'. Then make the fix."
SAME_TWICE = "The same failure happened twice: change your approach, do not repeat the last edit."
WHERE_LIMIT = 1500  # `flow-task where` and STATUS.md stay small enough to read in one look


def plans_dir():
    return engine.ROOT / "plans"


def repo():
    """The project a plan targets: $FLOW_WORKDIR, else the git repo of the caller's cwd (never silently FLOW_SKILL itself)."""
    if os.environ.get("FLOW_WORKDIR"):
        return os.environ["FLOW_WORKDIR"]
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    except OSError:
        top = ""
    if not top:
        raise PlanError("run inside the project's git repo (or set FLOW_WORKDIR)")
    return top


def git(cwd, *a, check=True):
    p = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise PlanError(f"git {' '.join(a[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def redact(text):
    """Mask likely secrets (keys, tokens, passwords, private key blocks) in text; the surrounding words stay."""
    text = "" if text is None else str(text)
    for rx, repl in SECRET_PATTERNS:
        text = rx.sub(repl, text)
    return text


def _atomic(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def extract_json(text):
    t = re.sub(r"^```[\w-]*\n|\n```\s*$", "", text.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise PlanError("reply contains no JSON object")
    try:
        return json.loads(t[i:j + 1])
    except ValueError as e:
        raise PlanError(f"invalid JSON: {e}")


def unsafe_path_reason(f):
    """Why a step file path is unsafe, or None. Checks escape (absolute, '..') and Windows-unsafe names, since a plan made on Linux may be merged on Windows."""
    if not f.strip():
        return "empty path"
    if Path(f).is_absolute() or f.startswith(("/", "\\")):
        return "absolute path"
    parts = [p for p in re.split(r"[\\/]+", f) if p not in ("", ".")]
    if ".." in parts:
        return "escapes the worktree"
    for p in parts:
        if WIN_BAD_CHARS.search(p):
            return f"Windows-invalid character in {p!r}"
        if p.endswith((".", " ")):
            return f"Windows-invalid name {p!r} (ends with dot or space)"
        if p.split(".", 1)[0].upper() in WIN_RESERVED:
            return f"Windows reserved device name {p!r}"
    return None


def escaping_paths(wt, rels):
    """Paths whose real location (symlinks followed) is outside the worktree wt."""
    root = os.path.realpath(wt)
    bad = []
    for r in rels:
        real = os.path.realpath(os.path.join(wt, r))
        if real != root and not real.startswith(root + os.sep):
            bad.append(r)
    return bad


def validate(plan, max_steps=None):
    """Normalise and check a plan: ids, required text, safe relative paths, existing deps, no cycles."""
    max_steps = max_steps or int(os.environ.get("FLOW_MAX_STEPS", "8"))
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        raise PlanError(f"plan needs 1..{max_steps} steps")
    ids = []
    for s in steps:
        if not isinstance(s, dict):
            raise PlanError("step is not an object")
        sid = str(s.get("id", ""))
        if not sid.strip() or not ID.match(sid) or sid in ids:
            raise PlanError(f"bad or duplicate step id {sid!r}")
        ids.append(sid)
        s["id"] = sid
        for k in ("title", "reason", "prompt"):
            if not isinstance(s.get(k), str) or not s[k].strip():
                raise PlanError(f"step {sid}: missing {k}")
        s["deps"] = [str(d) for d in (s.get("deps") or [])]
        s["files"] = [str(f) for f in (s.get("files") or [])]
        for f in s["files"]:
            why = unsafe_path_reason(f)
            if why:
                raise PlanError(f"step {sid}: unsafe path {f!r} ({why})")
        s["acceptance"] = str(s.get("acceptance") or "").strip()
    for s in steps:
        for d in s["deps"]:
            if d not in ids or d == s["id"]:
                raise PlanError(f"step {s['id']}: bad dependency {d!r}")
    left, done = {s["id"]: set(s["deps"]) for s in steps}, set()
    while left:  # Kahn: a round with no progress means a cycle
        ready = [i for i, d in left.items() if d <= done]
        if not ready:
            raise PlanError("dependency cycle")
        for i in ready:
            done.add(i)
            del left[i]
    return plan


def render_md(pid, plan, state):
    out = [f"# Plan {pid}", f"Goal: {plan['goal']}", f"Status: {state['status']}  | base {plan['base'][:8]}  | model: {state.get('parent_model') or 'default'}", ""]
    if state.get("dirty_files"):
        out += ["WARNING: the repo has uncommitted changes. Steps start from the base commit, so these are NOT part of the plan:"]
        out += [f"  {line}" for line in state["dirty_files"]] + [""]
    for s in plan["steps"]:
        out += [f"## [{s['id']}] {s['title']}", f"- why: {s['reason']}", f"- deps: {', '.join(s['deps']) or '-'}",
                f"- files: {', '.join(s['files']) or '-'}", f"- acceptance: `{s['acceptance'] or 'none'}`", ""]
    out.append(f"Approve: flow-task approve {pid}   (edit plans/{pid}/plan.json first if you want changes)")
    return "\n".join(out)


def load(pid):
    d = plans_dir() / pid
    if not (d / "state.json").is_file():
        raise PlanError(f"no such plan {pid}")
    return d, json.loads((d / "plan.json").read_text(encoding="utf-8")), json.loads((d / "state.json").read_text(encoding="utf-8"))


def _mirror(pid, **kv):
    try:
        r = engine.redis()
        r.call("HSET", f"plan:{pid}", *[x for k, v in kv.items() for x in (k, v)])
        r.call("EXPIRE", f"plan:{pid}", engine.TTL)
    except Exception:
        pass  # state.json is the truth; Redis is only for dashboards


def create(goal, parent_model=None):
    """Ask the planner agent for a plan, validate it, store it awaiting approval. Returns the plan id."""
    wd = repo()
    git(wd, "rev-parse", "--git-dir")
    if git(wd, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode:
        raise PlanError("repo has no commits: make an initial commit first")
    dirty = git(wd, "status", "--porcelain", check=False).stdout.splitlines()  # plans start from the commit, so uncommitted work is left out: say so
    pid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    d = plans_dir() / pid
    d.mkdir(parents=True)
    chain = agents.chain_from_env("claude:haiku", parent_model)
    ctx = bridge.recall(goal)
    prompt, trace, cost, plan = PLAN_PROMPT.format(max=os.environ.get("FLOW_MAX_STEPS", "8"), ctx=ctx, goal=goal), [], 0.0, None
    for _ in range(2):  # one repair round when the reply is not a valid plan
        res, c, _t = agents.call(chain, prompt, agents.Opts(wd, agents.READ_TOOLS, float(os.environ.get("FLOW_PLAN_USD", "0.30")), 300), trace)
        cost += c
        if not res.ok:
            raise PlanError(f"planner failed: {res.error}")
        try:
            plan = validate(extract_json(res.text))
            break
        except PlanError as e:
            prompt += f"\n\nYour previous reply was rejected ({e}). Reply with ONLY the corrected JSON object."
    if plan is None:
        raise PlanError("planner did not produce a valid plan")
    plan = {"goal": goal, "repo": wd, "base": git(wd, "rev-parse", "HEAD").stdout.strip(), "steps": plan["steps"]}  # repo is fixed here: approving from another cwd must not retarget it
    if dirty:
        plan["dirty_files"] = dirty[:20]
    state = {"id": pid, "status": "awaiting_approval", "goal": goal, "created": engine.now(), "parent_model": parent_model or os.environ.get("FLOW_PARENT_MODEL", ""),
             "plan_cost_usd": round(cost, 6), "plan_trace": trace, "steps": {s["id"]: {"status": "pending"} for s in plan["steps"]}}
    if dirty:
        state["dirty_files"] = dirty[:20]
    for e in trace:  # an agent error can echo an API key
        e["error"] = redact(e.get("error", ""))
    _atomic(d / "plan.json", json.dumps(plan, indent=2, ensure_ascii=False))
    _atomic(d / "state.json", json.dumps(state, indent=2, ensure_ascii=False))
    (d / "plan.md").write_text(render_md(pid, plan, state), encoding="utf-8")
    _mirror(pid, status="awaiting_approval", goal=goal[:200])
    return pid


def show(pid, limit=2800):
    d, plan, state = load(pid)
    md = render_md(pid, plan, state)
    return md if len(md.encode()) <= limit else md.encode()[:limit].decode(errors="ignore") + f"\n... (full: {d / 'plan.md'})"


def spawn(pid):
    d = plans_dir() / pid
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
    with open(d / "run.log", "ab") as log:
        subprocess.Popen([sys.executable, "-m", "flow_engine", "run", pid], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         cwd=str(engine.ROOT), env={**os.environ, "FLOW_HOME": str(engine.ROOT),
                         "PYTHONPATH": os.pathsep.join(filter(None, [str(Path(__file__).resolve().parent.parent), os.environ.get("PYTHONPATH")]))}, **kw)


def _hash(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()[:16]


def approve(pid, wait_for=False):
    """Freeze the (possibly hand-edited) plan by hash and start the runner in the background."""
    d, plan, state = load(pid)
    if state["status"] not in ("awaiting_approval",):
        raise PlanError(f"plan is {state['status']}, not awaiting approval")
    validate(plan)
    state.update(status="approved", approved=engine.now(), plan_hash=_hash(plan))
    state["steps"] = {s["id"]: state["steps"].get(s["id"], {"status": "pending"}) for s in plan["steps"]}
    _atomic(d / "state.json", json.dumps(state, indent=2, ensure_ascii=False))
    _mirror(pid, status="approved")
    if wait_for:
        return run(pid)
    spawn(pid)
    return "started"


def retry(pid):
    """Re-run a failed/partial plan: steps already merged stay, every other step starts again in a fresh worktree."""
    d, plan, state = load(pid)
    if state["status"] not in ("failed", "partial"):
        raise PlanError(f"plan is {state['status']}; only a failed or partial plan can be retried")
    for ev in state["steps"].values():
        if ev["status"] != "done":
            old = {k: ev[k] for k in ("status", "error") if ev.get(k)}
            ev.clear()
            ev.update(status="pending", previous=old)
    for k in ("integration", "error", "finished"):
        state.pop(k, None)
    state.update(status="approved", retries=state.get("retries", 0) + 1)
    _atomic(d / "state.json", json.dumps(state, indent=2, ensure_ascii=False))
    _mirror(pid, status="approved")
    spawn(pid)


def reject(pid):
    d, plan, state = load(pid)
    if state["status"] != "awaiting_approval":
        raise PlanError(f"plan is {state['status']}; only a plan awaiting approval can be rejected")
    state["status"] = "rejected"
    _atomic(d / "state.json", json.dumps(state, indent=2))
    _mirror(pid, status="rejected")


def status(pid):
    d, plan, state = load(pid)
    hb = state.get("heartbeat")
    return {"id": pid, "status": state["status"], "steps": {k: v["status"] for k, v in state["steps"].items()},
            "cost_usd": round(state.get("cost_usd", 0) + state.get("plan_cost_usd", 0), 6),
            "heartbeat_age_s": round(time.time() - hb) if hb and state["status"] == "running" else None,
            "report": str(d / "report.md") if (d / "report.md").is_file() else None}


class Ctx:
    def __init__(self, pid, d, plan, state):
        self.pid, self.d, self.plan, self.state = pid, d, plan, state
        self.repo, self.branch = plan.get("repo") or repo(), f"flow/{pid}"
        self.plan_wt = d / "wt" / "_plan"
        self.slock, self.glock, self.mlock = threading.Lock(), threading.Lock(), threading.Lock()
        self.chain = agents.chain_from_env("claude:haiku", state.get("parent_model"))
        self.budget = float(os.environ.get("FLOW_PLAN_MAX_USD", "3.0"))
        self.step_usd = float(os.environ.get("FLOW_STEP_USD", "0.40"))  # per-step estimate: what a launch reserves from the budget
        self.reserved = {}  # sid -> USD still reserved for that running step (spent is taken off it as the step pays)

    def save(self):
        for _ in range(5):
            try:  # steps mutate their dicts while the heartbeat thread serialises: retry the rare race
                with self.slock:
                    _atomic(self.d / "state.json", redact(json.dumps(self.state, indent=2, ensure_ascii=False)))
                return
            except RuntimeError:
                time.sleep(0.05)

    def add_cost(self, cost, tokens, sid=None):
        with self.slock:
            self.state["cost_usd"] = self.state.get("cost_usd", 0) + cost
            if sid in self.reserved:
                self.reserved[sid] = max(0.0, self.reserved[sid] - cost)
            t = self.state.setdefault("tokens", {})
            for k, v in tokens.items():
                t[k] = t.get(k, 0) + v

    def cap(self, sid):
        """USD this running step may still spend."""
        with self.slock:
            return self.reserved.get(sid, 0.0)

    def free(self):
        """USD neither spent nor reserved by a running step. Caller holds slock."""
        return self.budget - self.spent - sum(self.reserved.values())

    @property
    def spent(self):
        return self.state.get("cost_usd", 0)


_new_group, _kill_group = agents.new_group, agents.kill_group


def _tail(f):
    """Last ACCEPT_TAIL_BYTES of an output file; a huge output is never read into memory whole."""
    f.seek(0, os.SEEK_END)
    f.seek(max(0, f.tell() - ACCEPT_TAIL_BYTES))
    return f.read().decode("utf-8", errors="replace")


def accept(step, wt):
    cmd = step["acceptance"]
    if not cmd:
        return {"cmd": "", "ok": True, "skipped": True}
    # output goes to files, not pipes: a grandchild that outlives the kill cannot keep us waiting on an open pipe
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}  # the planner wrote this command: it gets no API keys
        p = subprocess.Popen(cmd, shell=True, cwd=wt, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, **_new_group())
        try:
            p.wait(timeout=int(os.environ.get("FLOW_ACCEPT_TIMEOUT", "300")))
        except subprocess.TimeoutExpired:
            _kill_group(p)
            return {"cmd": cmd, "ok": False, "rc": None, "tail": "timeout"}
        tail = redact((_tail(out) + _tail(err)).strip())[-600:]
    return {"cmd": cmd, "ok": p.returncode == 0, "rc": p.returncode, "tail": tail}


def _gate_line(g):
    return " ".join(f"{n}:{'skipped' if x.get('skipped') else ('ok' if x['ok'] else 'FAIL')}" for n, x in g["gates"].items())


def _cleanup(ctx, sid):
    wt = ctx.d / "wt" / sid
    git(ctx.repo, "worktree", "remove", "--force", str(wt), check=False)
    git(ctx.repo, "branch", "-D", f"flowstep/{ctx.pid}/{sid}", check=False)


def _junk(p):
    parts = Path(p).parts
    return "__pycache__" in parts or ".pytest_cache" in parts or p.endswith(".pyc")


def _touched(wt):
    """Files this attempt changed in the step worktree so far: tracked changes and new files, build junk left out."""
    names = git(wt, "diff", "--name-only", "HEAD", check=False).stdout.splitlines() + git(wt, "ls-files", "-o", "--exclude-standard", check=False).stdout.splitlines()
    return sorted({n for n in names if n and not _junk(n)})[:20]


def _drift(step, paths):
    """Committed files the plan did not list. A test file for a listed file (tests/test_<stem>.py) is expected; junk never counts."""
    declared = step["files"]
    if not declared:
        return []
    ok_tests = {f"test_{Path(f).stem}.py" for f in declared}
    return sorted(p for p in paths if p not in declared and not _junk(p) and not (p.startswith("tests/") and Path(p).name in ok_tests))


def _reflection(text):
    """The 'REFLECTION: ...' line an agent opened its reply with, redacted and cut to 300 chars, or ''."""
    m = re.search(r"^[ \t]*REFLECTION:.*$", text or "", re.M)
    return redact(m.group(0).strip())[:300] if m else ""


def _fail_hash(fb):
    """Identity of a failure: digits stripped, so a line number or a timestamp moving does not make a new failure."""
    return hashlib.sha256(re.sub(r"\d+", "", fb).encode()).hexdigest()[:12]


def _streak(prior):
    """How many identical failures in a row end the journal, and the hash of the last one. Any entry without a hash ends a streak."""
    n, last = 0, None
    for e in prior:
        h = e.get("hash")
        n = (n + 1 if h == last else 1) if h else 0
        last = h or None
    return n, last


def _attempt_failure(ev):
    """Why the newest attempt failed (agent error, failed gates or the acceptance tail), or ''."""
    if not ev.get("attempts"):
        return ""
    a = ev["attempts"][-1]
    if "agent_error" in a:
        return a["agent_error"]
    if not a.get("gates_ok", True):
        return f"gates [{a['gates']}]"
    ac = a.get("acceptance", {})
    if not ac.get("ok", True):
        lines = (ac.get("tail") or "").strip().splitlines()
        return f"acceptance rc={ac.get('rc')} {lines[-1] if lines else ''}"
    return ""


def do_step(ctx, step, rescued=False):
    sid = step["id"]
    wt, br = ctx.d / "wt" / sid, f"flowstep/{ctx.pid}/{sid}"  # not under flow/<pid>/: git refuses a ref that is also a directory
    ev = ctx.state["steps"][sid]
    with ctx.glock:  # git worktree add takes repo-level locks
        base_sha = git(ctx.plan_wt, "rev-parse", "HEAD").stdout.strip()
        _cleanup(ctx, sid)
        git(ctx.repo, "worktree", "add", "-q", "-b", br, str(wt), base_sha)
    ev.update(status="running", started=engine.now(), base=base_sha[:10], branch=br, attempts=[], api=ev.get("api", []))
    for k in ("stuck", "drift", "reflection"):  # per run: a retry or a resume starts these over
        ev.pop(k, None)
    ctx.save()
    t0 = time.time()
    done = {k: v for k, v in ctx.state["steps"].items() if v["status"] == "done"}
    dep_txt = "".join(f"Step [{d}] (already merged into your tree) reported: {done[d].get('summary', '')[:500]}\n" for d in step["deps"] if d in done)
    note = "\nOther steps were merged since you were planned; build on the code that is in this directory now.\n" if rescued else ""
    base_prompt = STEP_PROMPT.format(goal=ctx.plan["goal"], id=sid, title=step["title"], reason=step["reason"], files=", ".join(step["files"]) or "(not specified)",
                                     prompt=step["prompt"], deps=dep_txt + note, acceptance=step["acceptance"] or "none")
    base_prompt = bridge.recall(f"{step['title']} {step['prompt'][:200]}") + base_prompt
    # the worktree was just made fresh: an earlier run, retry or rescue of this step left its attempts in the journal, so the agent is told them
    prior = journal.read(ctx.pid, sid)
    streak, last_hash = _streak(prior)
    if prior:
        base_prompt = f"{EARLIER}\n{journal.brief(ctx.pid, sid)}\n\n{base_prompt}"
    prompt, passed, summary, error, first_fail, failed_names = base_prompt, False, "", "", "", ""
    if streak >= 2:
        prompt += f"\n\n{SAME_TWICE}"
    for n in range(1, int(os.environ.get("FLOW_RETRIES", "2")) + 2):
        cap = ctx.cap(sid)  # this step's reservation, minus what it has already paid
        if cap <= 0:
            error = f"plan budget ${ctx.budget} exhausted"
            break
        o = agents.Opts(str(wt), agents.WRITE_TOOLS, cap, int(os.environ.get("FLOW_TIMEOUT", "600")))
        res, cost, tok = agents.call(ctx.chain, prompt, o, ev["api"])
        ctx.add_cost(cost, tok, sid)
        ev["backend"] = res.backend or ev.get("backend", "")
        if not res.ok:
            error = redact(res.error)
            ev["attempts"].append({"n": n, "agent_error": redact(res.error)[:300]})
            journal.append(ctx.pid, sid, {"n": n, "status": "agent-error", "backend": res.backend, "error": res.error[:300]})
            break
        summary = redact(res.text.strip())
        reflection = _reflection(res.text)
        if reflection:
            ev["reflection"] = reflection
        g = gate.run_gates(str(wt))
        acc = accept(step, str(wt))
        ev["attempts"].append({"n": n, "gates": _gate_line(g), "gates_ok": g["ok"], "acceptance": acc})
        ctx.save()
        if g["ok"] and acc["ok"]:
            passed = True
            journal.append(ctx.pid, sid, {"n": n, "status": "passed", "backend": res.backend, "files": _touched(str(wt)), "summary": summary[:300], "reflection": reflection})
            break
        fb = f"Gates: {_gate_line(g)}\nAcceptance `{acc['cmd']}` rc={acc.get('rc')}:\n{acc.get('tail', '')[-500:]}"
        first_fail = first_fail or fb
        failed_names = ",".join([k for k, x in g["gates"].items() if not x.get("skipped") and not x["ok"]] + ([] if acc["ok"] else ["acceptance"]))
        fh = _fail_hash(fb)
        streak, last_hash = (streak + 1 if fh == last_hash else 1), fh
        journal.append(ctx.pid, sid, {"n": n, "status": "gate-failed" if not g["ok"] else "acceptance-failed", "backend": res.backend, "failed": failed_names.split(","),
                                      "files": _touched(str(wt)), "summary": summary[:300], "reflection": reflection, "hash": fh})
        if streak >= 3:  # the same failure three times in a row: another attempt would only repeat it
            error = "stuck: identical failure repeated 3 times"
            ev["stuck"] = True
            journal.append(ctx.pid, sid, {"n": n, "status": "stuck"})
            break
        hint = bridge.get_recipe(failed_names)  # reflection: what fixed this kind of failure before
        prompt = (f"{base_prompt}\n\nYour previous attempt did not pass the automated checks. Fix this:\n{fb}" + (f"\n\n{SAME_TWICE}" if streak == 2 else "")
                  + f"\n\n{REFLECT}" + (f"\n\nWorked before for the same failure:\n{hint}" if hint else ""))
        if hint:
            ev["recipe_used"] = failed_names
    ev.update(summary=redact(summary)[:1200], duration_s=round(time.time() - t0, 1))
    # a code-level failure happened: keep the lesson (API errors are not lessons); a failure never overwrites a recipe that worked
    if first_fail and failed_names and (passed or not bridge.get_recipe(failed_names)):
        bridge.record_recipe(failed_names, redact(f"step '{step['title'][:60]}': {'fixed after' if passed else 'still failing after'} {len(ev['attempts'])} attempts; first failure {first_fail[:300]}"))
        ev["recipe_recorded"] = failed_names
    if not passed:
        ev["uncommitted"] = git(str(wt), "status", "--porcelain", check=False).stdout.strip().splitlines()[:20]
        ev.update(status="failed", error=error or "gates/acceptance still failing after retries", worktree=str(wt))
        ctx.save()
        return False
    # plain `add -A` (honours .gitignore); an exclude-pathspec makes git fail when .gitignore also lists the excluded path
    git(str(wt), "add", "-A")
    git(str(wt), "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *JUNK)
    changed = git(str(wt), "diff", "--cached", "--name-only").stdout.strip()
    if not changed and not step["acceptance"]:
        ev.update(status="failed", error="agent made no changes and the step has no acceptance command")
        ctx.save()
        return False
    if changed:
        git(str(wt), *IDENT, "commit", "-q", "-m", f"step {sid}: {step['title']}")
    sha = git(str(wt), "rev-parse", "HEAD").stdout.strip()
    ns = git(str(wt), "diff", "--name-status", f"{base_sha}..{sha}").stdout.strip().splitlines()
    num = {l.split("\t")[2]: (l.split("\t")[0], l.split("\t")[1]) for l in git(str(wt), "diff", "--numstat", f"{base_sha}..{sha}").stdout.strip().splitlines()}
    files = []
    for l in ns:
        st, path = l.split("\t", 1)[0], l.split("\t")[-1]
        files.append({"status": st, "path": path, "added": num.get(path, ("?", "?"))[0], "removed": num.get(path, ("?", "?"))[1]})
    bad_links = escaping_paths(str(wt), [f["path"] for f in files])  # a symlink to outside the worktree must not be merged
    if bad_links:
        ev.update(status="failed", error=f"changed path leads outside the step worktree (symlink): {bad_links[:5]}", worktree=str(wt))
        ctx.save()
        return False
    declared = set(step["files"])
    ev.update(files=files, commit=sha[:10], outside_plan=sorted(f["path"] for f in files if declared and f["path"] not in declared))
    ev["drift"] = _drift(step, [f["path"] for f in files])  # warning only: the merge goes ahead
    if ev["drift"]:
        journal.append(ctx.pid, sid, {"n": len(ev["attempts"]), "status": "drift", "drift": ev["drift"]})
    with ctx.mlock:
        m = git(ctx.plan_wt, *IDENT, "merge", "--no-ff", "-q", "-m", f"merge step {sid}: {step['title']}", br, check=False)
        if m.returncode:
            git(ctx.plan_wt, "merge", "--abort", check=False)
    if m.returncode:
        ev.setdefault("rescues", []).append("merge conflict with an earlier step")
        _cleanup(ctx, sid)
        if rescued:
            ev.update(status="failed", error="merge conflict again after re-running on the updated tree")
            ctx.save()
            return False
        ev["rescues"][-1] += ": re-running the step on the updated tree"
        return do_step(ctx, step, rescued=True)
    ev.update(status="done", merged=engine.now())
    _cleanup(ctx, sid)
    ctx.save()
    return True


def _launchable(ctx, running_files):
    st = ctx.state["steps"]
    out, skip = [], []
    for s in ctx.plan["steps"]:
        if st[s["id"]]["status"] != "pending":
            continue
        if any(st[d]["status"] in ("failed", "skipped") for d in s["deps"]):
            skip.append(s)
        elif all(st[d]["status"] == "done" for d in s["deps"]) and not (set(s["files"]) & running_files):
            out.append(s)
    return out, skip


def run(pid):
    d, plan, state = load(pid)
    if state.get("plan_hash") and _hash(plan) != state["plan_hash"]:  # approval froze this plan: an edited plan.json must not run (or resume)
        state.update(status="failed", error="plan.json changed after approval: not running it. Re-plan and approve again", finished=engine.now())
        _atomic(d / "state.json", json.dumps(state, indent=2, ensure_ascii=False))
        raise PlanError(state["error"])
    lock = engine.try_lock(d / "run.lock")
    if not lock:
        return "already running"
    if state["status"] not in ("approved", "running"):
        raise PlanError(f"plan is {state['status']}")
    ctx = Ctx(pid, d, plan, state)
    ctx.plan_wt.parent.mkdir(exist_ok=True)
    if not ctx.plan_wt.exists():
        git(ctx.repo, "worktree", "add", "-q", "-b", ctx.branch, str(ctx.plan_wt), plan["base"])
    for sid, ev in state["steps"].items():  # resume: a step caught mid-run by a crash starts over
        if ev["status"] in ("running", "queued"):  # queued = picked, killed before its first save: _launchable only takes pending
            _cleanup(ctx, sid)
            ev.update(status="pending")
            ev.setdefault("rescues", []).append("runner died mid-step: restarted")
    state.update(status="running", started=state.get("started") or engine.now(), heartbeat=time.time())
    ctx.save()
    _mirror(pid, status="running")
    stop = threading.Event()

    def beat():
        while not stop.wait(5):
            state["heartbeat"] = time.time()
            ctx.save()

    threading.Thread(target=beat, daemon=True).start()
    par, futures, t0 = int(os.environ.get("FLOW_PARALLEL", "3")), {}, time.time()
    steps = {s["id"]: s for s in plan["steps"]}
    try:
        with ThreadPoolExecutor(par) as pool:
            while True:
                running_files = {f for sid in futures.values() for f in steps[sid]["files"]}
                ready, skip = _launchable(ctx, running_files)
                for s in skip:
                    state["steps"][s["id"]].update(status="skipped", error="a dependency failed")
                for s in ready:
                    if len(futures) >= par:
                        break
                    with ctx.slock:  # reserve the per-step estimate before launch, so parallel steps cannot together overshoot the budget
                        if ctx.free() < ctx.step_usd:
                            if futures:
                                break  # running steps release their reservation when they finish
                            state["steps"][s["id"]].update(status="failed", error=f"plan budget ${ctx.budget} cannot cover the per-step reserve ${ctx.step_usd}")
                            continue
                        ctx.reserved[s["id"]] = ctx.step_usd
                    state["steps"][s["id"]]["status"] = "queued"
                    futures[pool.submit(do_step, ctx, s)] = s["id"]
                    running_files |= set(s["files"])
                ctx.save()
                if not futures:
                    break
                fin, _ = wait(list(futures), return_when=FIRST_COMPLETED)
                for f in fin:
                    sid = futures.pop(f)
                    with ctx.slock:
                        ctx.reserved.pop(sid, None)  # release: the step's real cost is already in spent
                    try:
                        f.result()
                    except Exception as e:  # a crash inside a step must not take the whole plan down
                        state["steps"][sid].update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
                    engine.log(json.dumps({"ts": engine.now(), "event": "plan_step", "plan": pid, "step": sid, "status": state["steps"][sid]["status"]}))
                    ctx.save()
                    write_status(pid)
        state["duration_s"] = round(state.get("duration_s", 0) + time.time() - t0, 1)
        sts = [v["status"] for v in state["steps"].values()]
        state["integration"] = integrate(ctx) if "done" in sts else None  # nothing merged = nothing to integrate
        state["status"] = "done" if all(x == "done" for x in sts) and state["integration"]["ok"] else ("partial" if "done" in sts else "failed")
    except Exception as e:
        state.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
    finally:
        stop.set()
        state["finished"] = engine.now()
        ctx.save()
        report(pid)
        write_status(pid)
        _mirror(pid, status=state["status"], cost_usd=round(ctx.spent, 6))
        engine.log(json.dumps({"ts": engine.now(), "event": "plan", "plan": pid, "status": state["status"], "cost_usd": round(ctx.spent, 6)}))
        lock.close()
    return state["status"]


def integrate(ctx):
    """Re-run every merged step's acceptance on the merged tree: proves the pieces work together, not just alone."""
    checks = []
    for s in ctx.plan["steps"]:
        if ctx.state["steps"][s["id"]]["status"] == "done" and s["acceptance"]:
            checks.append({"step": s["id"], **accept(s, str(ctx.plan_wt))})
    g = gate.run_gates(str(ctx.plan_wt))
    return {"ok": all(c["ok"] for c in checks) and g["ok"], "acceptance": checks, "gates": _gate_line(g), "head": git(ctx.plan_wt, "rev-parse", "--short", "HEAD").stdout.strip()}


def resume_stale():
    """Restart plan runners whose heartbeat stopped (process killed, machine slept) while nobody holds the run lock."""
    stale = float(os.environ.get("FLOW_STALE", "120"))
    for sf in plans_dir().glob("*/state.json"):
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
            if st["status"] == "running" and time.time() - st.get("heartbeat", 0) > stale:
                lk = engine.try_lock(sf.parent / "run.lock")
                if lk:
                    lk.close()
                    print(f"resuming stale plan {st['id']}", flush=True)
                    spawn(st["id"])
        except Exception as e:
            print(f"resume check failed for {sf.parent.name}: {e}", flush=True)


def report(pid):
    """Write report.md/json: per step what was done, which files, why, and the evidence the harness itself collected."""
    d, plan, st = load(pid)
    steps = {s["id"]: s for s in plan["steps"]}
    L = [f"# Báo cáo kế hoạch {pid}: {st['status'].upper()}", "", f"- Mục tiêu: {plan['goal']}", f"- Branch để review: `flow/{pid}` (base `{plan['base'][:10]}`)",
         f"- Model/backend: {st.get('parent_model') or 'default'} | chain thực tế: {sorted({v.get('backend', '') for v in st['steps'].values() if v.get('backend')})}",
         f"- Chi phí: ${st.get('cost_usd', 0) + st.get('plan_cost_usd', 0):.4f} | thời gian: {st.get('duration_s', '?')}s | plan hash `{st.get('plan_hash', '')}`", ""]
    pt = [e for e in st.get("plan_trace", []) if e["action"] != "done"]
    L += [f"- Lập kế hoạch bởi: {[e['backend'] for e in st.get('plan_trace', []) if e['action'] == 'done']}"] + [f"  - API {e['backend']} lần {e['attempt']}: {e['error'][:120]} → {e['action']}" for e in pt]
    L += ["",
         "| Bước | Trạng thái | File | Lần thử | Retry/fallback API |", "|---|---|---|---|---|"]
    for sid, ev in st["steps"].items():
        api = [e for e in ev.get("api", []) if e["action"] != "done"]
        L.append(f"| {sid} {steps[sid]['title']} | {ev['status']} | {len(ev.get('files', []))} | {len(ev.get('attempts', []))} | {len(api)} |")
    for sid, ev in st["steps"].items():
        s = steps[sid]
        L += ["", f"## [{sid}] {s['title']}: {ev['status']}", f"**Việc làm:** {ev.get('summary') or ev.get('error') or '(không chạy)'}", "", "**File:**"]
        L += [f"- `{f['status']}` {f['path']} (+{f['added']} -{f['removed']})" for f in ev.get("files", [])] or ["- (không có thay đổi đã merge)"]
        L += [f"- chưa commit trong worktree `{ev.get('worktree')}`: {u}" for u in ev.get("uncommitted", [])]
        if ev.get("outside_plan"):
            L.append(f"- CẢNH BÁO ngoài kế hoạch: {', '.join(ev['outside_plan'])}")
        L += ["", f"**Lý do:** {s['reason']}", "", "**Bằng chứng:**"]
        if ev.get("commit"):
            L.append(f"- commit `{ev['commit']}` merged vào `flow/{pid}`")
        for a in ev.get("attempts", []):
            if "agent_error" in a:
                L.append(f"- lần {a['n']}: agent lỗi: {a['agent_error']}")
            else:
                ac = a["acceptance"]
                L.append(f"- lần {a['n']}: gates [{a['gates']}]; acceptance `{ac['cmd'] or 'none'}` rc={ac.get('rc')} {'OK' if ac['ok'] else 'FAIL'}" + (f"\n  ```\n  {ac.get('tail', '')[-300:]}\n  ```" if ac.get("tail") else ""))
        for e in ev.get("api", []):
            if e["action"] != "done":
                L.append(f"- API {e['backend']} lần {e['attempt']}: {e['error'][:120]} → {e['action']}")
        for r in ev.get("rescues", []):
            L.append(f"- cứu: {r}")
        if ev.get("recipe_used"):
            L.append(f"- reflection: dùng recipe cũ `flow-gate:{ev['recipe_used']}` ở lần thử lại")
        if ev.get("recipe_recorded"):
            L.append(f"- reflection: đã ghi recipe `flow-gate:{ev['recipe_recorded']}`")
        if ev.get("attempts"):
            flags = [f"attempts {len(ev['attempts'])}"] + (["stuck"] if ev.get("stuck") else []) + ([f"drift: {', '.join(ev['drift'])}"] if ev.get("drift") else [])
            L.append("- reflection: " + " | ".join(flags))
        if ev.get("reflection"):
            L.append(f"- {ev['reflection']}")
        if ev.get("error"):
            L.append(f"- lỗi: {ev['error']}")
    integ = st.get("integration")
    if integ:
        L += ["", "## Kiểm tra tích hợp (chạy lại trên branch đã merge)", f"- kết quả: {'OK' if integ['ok'] else 'FAIL'}; gates [{integ['gates']}]; head `{integ['head']}`"]
        L += [f"- `{c['cmd']}` ({c['step']}) rc={c.get('rc')} {'OK' if c['ok'] else 'FAIL'}" for c in integ["acceptance"]]
    L += ["", "## Cách review", f"- `git diff {plan['base'][:10]}..flow/{pid}` xem toàn bộ thay đổi", f"- `git merge flow/{pid}` khi đồng ý; `git worktree remove plans/{pid}/wt/_plan` để dọn"]
    (d / "report.md").write_text(redact("\n".join(L) + "\n"), encoding="utf-8")
    _atomic(d / "report.json", redact(json.dumps({"plan": plan, "state": st}, indent=2, ensure_ascii=False)))
    return d / "report.md"


def _next_action(pid, st, d):
    if st == "awaiting_approval":
        return f"flow-task approve {pid}  (or edit plans/{pid}/plan.json first)"
    if st in ("approved", "running"):
        return f"wait; check with flow-task status {pid}"
    if st in ("failed", "partial"):
        return f"flow-task retry {pid}  (merged steps stay, failed ones run again)"
    if st == "done":
        return f"review flow/{pid}; report: {d / 'report.md'}"
    return f"none (plan is {st})"


def where_text(pid, plan_, state, d, limit=WHERE_LIMIT):
    """Where a plan stands, one step per line and one Next action; the same text goes to `flow-task where` and STATUS.md."""
    st = state["status"]
    head = [f"Plan {pid}: {st}  branch flow/{pid}", f"Goal: {' '.join(plan_['goal'].split())[:200]}"]
    steps = []
    for s in plan_["steps"]:
        ev = state["steps"].get(s["id"], {})
        why = ev.get("error", "") if ev.get("stuck") else (_attempt_failure(ev) or ev.get("error", ""))
        why = " ".join(why.split())[:80]
        steps.append(f"{s['id']} {ev.get('status', 'pending')} {len(ev.get('attempts', []))} {why}".rstrip())
    nxt = f"Next: {_next_action(pid, st, d)}"
    budget = limit - len(nxt.encode("utf-8")) - 8  # room for the "..." marker that a long plan gets
    out, used = [], 0
    for line in head + steps:
        size = len(line.encode("utf-8")) + 1
        if used + size > budget:
            out.append("...")
            break
        out.append(line)
        used += size
    return "\n".join(out + [nxt])


def latest_here():
    """Newest plan of the caller's repo. Plan ids start with a timestamp, so reverse name order is newest first."""
    here = os.path.realpath(repo())
    for sf in sorted(plans_dir().glob("*/state.json"), reverse=True):
        try:
            p = json.loads((sf.parent / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if p.get("repo") and os.path.realpath(p["repo"]) == here:
            return sf.parent.name
    raise PlanError('no plan for this repo yet; start one with: flow-task plan "<goal>"')


def where(pid=None):
    """`flow-task where`: the given plan, else the newest plan of the caller's repo."""
    pid = pid or latest_here()
    d, plan_, state = load(pid)
    return where_text(pid, plan_, state, d)


def write_status(pid):
    """STATUS.md next to state.json: the `where` text, so a new or compacted session reads one small file."""
    d, plan_, state = load(pid)
    try:
        _atomic(d / "STATUS.md", where_text(pid, plan_, state, d))
    except OSError:
        pass  # a status file is a convenience; state.json is the truth
