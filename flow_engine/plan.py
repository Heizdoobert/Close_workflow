"""plan -> human approval -> sub-agents in isolated git worktrees (parallel where independent) -> evidence report.

Layout, per plan: <ROOT>/plans/<pid>/{plan.json, plan.md, state.json, STATUS.md, run.log, run.lock, report.md, report.json, journal/, wt/}
Git: branch flow/<pid> holds the merged result; each step works on flowstep/<pid>/<step> in its own worktree and is merged
only after its gates and acceptance command pass. Nothing touches your current branch: you review and merge flow/<pid>.
state.json is the source of truth (Redis is a best-effort mirror), so a crashed runner can be resumed.
journal/<sid>.jsonl keeps one line per attempt, so a retried or resumed step knows what it already tried.
"""

import datetime
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

from flow_engine import agents, audit, bridge, engine, gate, interview, journal, method
# SECRET_PATTERNS and redact live in secrets.py; they stay importable from here
from flow_engine.secrets import SECRET_PATTERNS, redact, redact_obj, redact_shapes

ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
IDENT = ["-c", "user.name=flow-engine", "-c", "user.email=flow@localhost"]
JUNK = [":(glob)**/__pycache__/**", ":(glob)**/*.pyc", ":(glob).pytest_cache/**"]  # build junk a repo without a .gitignore would otherwise commit
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
WIN_BAD_CHARS = re.compile(r'[<>:"|?*]')
SECRET_ENV = re.compile(r"(?i)^(?!GIT_CONFIG_KEY_\d+$).*(KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH)")  # GIT_CONFIG_KEY_n is a config name: dropping it leaves GIT_CONFIG_COUNT pointing at nothing and every git call fails
ACCEPT_TAIL_BYTES = 64 * 1024  # per stream; only the last part is kept for the report and the retry prompt
class PlanError(Exception):
    pass


PLAN_PROMPT = """You are the planning agent of an automated engineering system. Explore the repository read-only, then reply with ONLY one JSON object, no prose, no code fences:
{{{shape},"steps":[{{"id":"s1","title":"short imperative","reason":"why this step is needed for the goal","deps":[],"files":["relative/paths/this/step/creates/or/edits"],"acceptance":"ONE shell command that exits 0 only if this step is really done (e.g. python3 -m pytest -q tests/test_x.py; always `python3 -m pytest`, never a bare `pytest`), run from the repo root","prompt":"self-contained instructions for the engineer doing this step"}}]}}
Rules: at most {max} steps. Steps that do not depend on each other run in parallel in separate git worktrees, so give them disjoint files. "deps" lists step ids that must finish first. The acceptance command must verify behaviour, not just that a file exists. Stay inside the repo. Every step is done by a cheap hands-on model (searching, reading, writing code and tests, wiring files together); you, the planner, do all the thinking. So each "prompt" must spell out every file, signature, algorithm step and edge case, as many small mechanical operations, never a design choice left open.
{rules}
{ctx}Goal: {goal}"""

STEP_PROMPT = """You are a sub-agent doing ONE step of a larger plan, inside an isolated git worktree (your current directory). Do only this step.
Overall goal: {goal}
{analysis}
Your step [{id}] {title}
Why: {reason}
Files you are expected to touch: {files}
Instructions: {prompt}
Do not rename, move or delete a path, or remove a public function/class other code uses, unless it is in your files list (check callers with Grep first).
Finish the work: no stubs (a function that only passes), no TODO or debug leftovers, no swallowed exceptions, no test without an assertion, no eval/shell=True/SQL built from strings.
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


def _mode_lost(path):
    """True when the filesystem under `path` shows every file as executable (NTFS/FAT/exFAT through FUSE): git then sees a mode change in every worktree file."""
    import tempfile
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path) as f:
        os.chmod(f.name, 0o644)
        return bool(os.stat(f.name).st_mode & 0o111)


def _nomode():
    """Worktrees live under plans/, which can be on another filesystem than the repo. If that one cannot keep file modes, every checked-out file reads as `old mode 100644 new mode 100755`: the plan
    worktree turns dirty, `git merge` refuses and every step fails. Real run: repo on ext4, FLOW_SKILL on NTFS. The env var reaches every git call here, the gates and the agents. Cost: a mode change
    (chmod +x) made by a step is not committed on such a filesystem, which could not keep it anyway."""
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    if any(os.environ.get(f"GIT_CONFIG_KEY_{i}") == "core.fileMode" for i in range(n)) or not _mode_lost(plans_dir()):
        return
    os.environ.update({"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "core.fileMode", f"GIT_CONFIG_VALUE_{n}": "false"})


def git(cwd, *a, check=True):
    p = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and p.returncode:
        raise PlanError(f"git {' '.join(a[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p


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
        return json.loads(t[i:j + 1], strict=False)  # strict=False: models put raw newlines/tabs inside strings
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


TRIVIAL_SEGMENT = re.compile(r"(true|:|exit(\s+\d+)?|(echo|printf)(\s.*)?)")


def trivial_acceptance_reason(cmd):
    """Why an acceptance command proves nothing ('' when it can really fail): empty, or every segment is true, :, exit N, echo or printf."""
    segments = [s.strip() for s in re.split(r"&&|\|\||;|\|", cmd or "")]
    segments = [s for s in segments if s]
    if not segments:
        return "empty acceptance command"
    if all(TRIVIAL_SEGMENT.fullmatch(s) for s in segments):
        return f"trivial acceptance command {cmd.strip()!r} proves nothing"
    return ""


def validate(plan, max_steps=None):
    """Normalise and check a plan: ids, required text, safe relative paths, existing deps, no cycles, real acceptance."""
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
        s["kind"] = "general" if s.get("kind") == "general" else "code"  # picks the model: code steps -> GPT-OSS first, the rest -> Gemini
        s["acceptance"] = str(s.get("acceptance") or "").strip()
        why = trivial_acceptance_reason(s["acceptance"])
        if why:
            raise PlanError(f"step {sid}: {why}")
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
    out += interview.md(plan.get("brief")) + method.md(plan.get("analysis"))
    if state.get("dirty_files"):
        out += ["WARNING: the repo has uncommitted changes. Steps start from the base commit, so these are NOT part of the plan:"]
        out += [f"  {line}" for line in state["dirty_files"]] + [""]
    for s in plan["steps"]:
        out += [f"## [{s['id']}] {s['title']}", f"- why: {s['reason']}", f"- deps: {', '.join(s['deps']) or '-'}",
                f"- files: {', '.join(s['files']) or '-'}", f"- kind: {s.get('kind', 'code')}", f"- acceptance: `{s['acceptance'] or 'none'}`", ""]
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


def _sum_tokens(calls):
    """Per-counter total of the ledger rows. state["tokens"] starts from the planning calls (map, planner) so it agrees with state["calls"]."""
    t = {}
    for c in calls:
        for k, v in c.items():
            if isinstance(v, (int, float)):
                t[k] = t.get(k, 0) + v
    return t


def create(goal, parent_model=None, brief=None):
    """Ask the planner agent for a plan, validate it, store it awaiting approval. Returns the plan id.
    `brief` = what the interview step (flow_engine/interview.py) cleared with the user; it must be complete and confirmed."""
    if brief is not None:
        bad = interview.brief_problems(brief)
        if bad:
            raise PlanError("; ".join(bad))
        brief = interview.clean(brief)
    wd = repo()
    git(wd, "rev-parse", "--git-dir")
    if git(wd, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode:
        raise PlanError("repo has no commits: make an initial commit first")
    dirty = git(wd, "status", "--porcelain", check=False).stdout.splitlines()  # plans start from the commit, so uncommitted work is left out: say so
    pid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    d = plans_dir() / pid
    d.mkdir(parents=True)
    chain = agents.chain_from_env("claude:haiku", parent_model, "general")
    ctx = (interview.text(brief) if brief else "") + bridge.recall(goal)
    trace, cost, calls = [], 0.0, []
    code_chain = agents.chain_from_env("claude:haiku", parent_model, "code")
    if os.environ.get("FLOW_MAP", "1") == "1" and code_chain[:1] != chain[:1]:  # the hands search the repo, the head plans from their quoted facts
        res, c, tok = agents.call(code_chain, method.map_prompt(goal), agents.Opts(wd, agents.READ_TOOLS, float(os.environ.get("FLOW_MAP_USD", "0.10")), 300), trace)
        cost += c
        calls.append({"role": "map", "backend": res.backend, **tok})
        if not res.ok:  # Gemini must get its hands from GPT-OSS: no GPT-OSS, no plan
            raise PlanError(f"GPT-OSS map call failed: {redact(res.error)[:200]}")
        try:
            ctx += method.scout_text(method.parse_scout(extract_json(res.text), wd).get("facts"), 5000).replace("read the merged result", "searched the repository for this goal")
        except PlanError:
            pass  # unusable reply: the planner explores by itself
    base, trace, cost, plan, why, calls = PLAN_PROMPT.format(max=os.environ.get("FLOW_MAX_STEPS", "8"), shape=method.SHAPE, rules=method.RULES, ctx=ctx, goal=goal), trace, cost, None, "", calls
    for _ in range(max(1, int(os.environ.get("FLOW_PLAN_ROUNDS", "4")))):  # repair rounds when the reply is not a valid plan (weaker models need more than one)
        prompt = base + (f"\n\nYour previous reply was rejected ({why}). Reply with ONLY the corrected JSON object." if why else "")
        res, c, tok = agents.call(chain, prompt, agents.Opts(wd, agents.READ_TOOLS, float(os.environ.get("FLOW_PLAN_USD", "0.30")), 300), trace)
        cost += c
        calls.append({"role": "planner", "backend": res.backend, **tok})
        if not res.ok:
            raise PlanError(f"planner failed: {res.error}")
        try:
            cand = validate(extract_json(res.text))
            bad = method.problems(cand.get("analysis"), lambda f: git(wd, "cat-file", "-e", f"HEAD:{f}", check=False).returncode == 0)  # research must cite files that exist
            if bad:
                raise PlanError("; ".join(bad))
            plan = cand
            break
        except PlanError as e:
            why = str(e)
    if plan is None:
        raise PlanError(f"planner did not produce a valid plan: {why}")
    plan = {"goal": goal, "repo": wd, "base": git(wd, "rev-parse", "HEAD").stdout.strip(), **({"brief": brief} if brief else {}), "analysis": method.clean(plan["analysis"]), "steps": plan["steps"]}  # repo is fixed here: approving from another cwd must not retarget it
    if dirty:
        plan["dirty_files"] = dirty[:20]
    state = {"id": pid, "status": "awaiting_approval", "goal": goal, "created": engine.now(), "parent_model": parent_model or os.environ.get("FLOW_PARENT_MODEL", ""),
             "plan_cost_usd": round(cost + (brief or {}).get("cost_usd", 0), 6), "plan_trace": trace, "calls": calls, "tokens": _sum_tokens(calls), "steps": {s["id"]: {"status": "pending"} for s in plan["steps"]}}
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


def _hash(plan, dirty=()):
    """Approval hash. Dirty files that were accepted at approve are part of it: the approval covers what the user saw."""
    payload = {"plan": plan, "dirty": list(dirty)} if dirty else plan  # no dirty list: same hash as before this check existed
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def overlaps(pid, plan):
    """Other approved or running plans on the same repo that list any of this plan's files. Only a warning: both still run,
    and the conflict shows up when the branches are merged."""
    mine = {Path(f).as_posix() for s in plan["steps"] for f in s["files"]}
    out = []
    for pf in sorted(plans_dir().glob("*/plan.json")):
        other = pf.parent.name
        if other == pid:
            continue
        try:
            op = json.loads(pf.read_text(encoding="utf-8"))
            ost = json.loads((pf.parent / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # a half-written or foreign folder is not a plan we can compare with
        if op.get("repo") != plan.get("repo") or ost.get("status") not in ("approved", "running"):
            continue
        shared = sorted(mine & {Path(f).as_posix() for s in op.get("steps", []) for f in s.get("files", [])})
        if shared:
            out.append({"plan": other, "files": shared})
    return out


def approve(pid, wait_for=False, allow_dirty=False):
    """Freeze the (possibly hand-edited) plan by hash and start the runner in the background.
    Uncommitted files in the repo are not seen by the steps (they run from the commit), so approve refuses them unless `allow_dirty`;
    with it, the list is recorded in state and part of plan_hash."""
    d, plan, state = load(pid)
    if state["status"] not in ("awaiting_approval",):
        raise PlanError(f"plan is {state['status']}, not awaiting approval")
    validate(plan)
    dirty = git(plan["repo"], "status", "--porcelain", check=False).stdout.splitlines()[:20]
    if dirty and not allow_dirty:
        raise PlanError("repo has uncommitted changes the steps will not see (they run from the commit): " + ", ".join(dirty)
                        + ". Commit them, or approve with --allow-dirty to run from the commit anyway")
    state.update(status="approved", approved=engine.now(), plan_hash=_hash(plan, dirty), overlap=overlaps(pid, plan))
    if dirty:
        state["dirty_approved"] = dirty
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


CLEANABLE = ("done", "failed", "partial", "rejected")


def _clean_plan(d, plan_, result_branches):
    """Remove one finished plan's worktrees and branches. Returns one line per item actually removed."""
    pid, rp = d.name, plan_.get("repo")
    if not rp or not Path(rp).is_dir():
        return []
    out = []

    def drop(args, what):
        if git(rp, *args, check=False).returncode == 0:
            out.append(f"{pid}: {what}")

    wt_dir = d / "wt"
    names = sorted(p.name for p in wt_dir.iterdir()) if wt_dir.is_dir() else []
    for name in names:  # step worktrees and the plan worktree (_plan); the branch flow/<pid> survives the worktree
        drop(["worktree", "remove", "--force", str(wt_dir / name)], f"worktree wt/{name}")
    step_ids = {s["id"] for s in plan_["steps"]} | (set(names) - {"_plan"})
    for sid in sorted(step_ids):
        drop(["branch", "-D", f"flowstep/{pid}/{sid}"], f"branch flowstep/{pid}/{sid}")
    if result_branches:
        drop(["branch", "-D", f"flow/{pid}"], f"branch flow/{pid}")
    try:
        wt_dir.rmdir()  # only when empty
    except OSError:
        pass
    return out


def clean(older_than_days=None, result_branches=False):
    """Remove finished plans' step worktrees and flowstep/* branches. Candidates: done, failed, partial or rejected plans
    that no runner holds; with older_than_days, only those finished at least that many days ago. flow/<pid> is kept unless
    result_branches. plan.json, state.json and report.md stay. Returns one line per removed worktree or branch."""
    cutoff = time.time() - older_than_days * 86400 if older_than_days is not None else None
    out, repos = [], set()
    for sf in sorted(plans_dir().glob("*/state.json")):
        d = sf.parent
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
            if st.get("status") not in CLEANABLE:
                continue  # approved, running and awaiting_approval plans are never touched
            if cutoff is not None and (not st.get("finished") or _epoch(st["finished"]) > cutoff):
                continue  # no finish time (e.g. rejected) cannot prove it is old enough
            plan_ = json.loads((d / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        lk = engine.try_lock(d / "run.lock")
        if not lk:
            continue  # a runner still holds the plan
        try:
            out += _clean_plan(d, plan_, result_branches)
            if plan_.get("repo"):
                repos.add(plan_["repo"])
        finally:
            lk.close()
    for rp in sorted(repos):
        if Path(rp).is_dir():
            git(rp, "worktree", "prune", check=False)
    return out


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
        self.chain = agents.chain_from_env("claude:haiku", state.get("parent_model"), "general")  # planner-side work: non-code steps, review, SHIP
        self.code_chain = agents.chain_from_env("claude:haiku", state.get("parent_model"), "code")
        self.budget = float(os.environ.get("FLOW_PLAN_MAX_USD", "3.0"))
        self.step_usd = float(os.environ.get("FLOW_STEP_USD", "0.40"))  # per-step estimate: what a launch reserves from the budget
        self.reserved = {}  # sid -> USD still reserved for that running step (spent is taken off it as the step pays)

    def save(self):
        for _ in range(5):
            try:  # steps mutate their dicts while the heartbeat thread serialises: retry the rare race
                with self.slock:
                    _atomic(self.d / "state.json", json.dumps(redact_obj(self.state), indent=2, ensure_ascii=False))
                return
            except RuntimeError:
                time.sleep(0.05)

    def add_cost(self, cost, tokens, sid=None, role="", backend=""):
        """role + backend put the call in state["calls"]: who spent which tokens on which model."""
        with self.slock:
            if role:
                self.state.setdefault("calls", []).append({"role": role, "backend": backend, **tokens})
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


def _gate_details(g):
    """What the failed gates found (security, code_verify and blast_radius name file:line and rule), so a retry knows what to fix."""
    bits = [f"{n}: {'; '.join(x['findings'][:5])}" for n, x in g["gates"].items() if not x["ok"] and x.get("findings")]
    return ("\nFindings: " + " | ".join(bits))[:900] if bits else ""


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


def _track_group(ctx, ev, pgid, started):
    """Record an agent's process group in state.json, so a runner killed mid-call leaves a trail that recover() can follow."""
    started.append(pgid)
    with ctx.slock:
        ev.setdefault("agent_pgids", []).append(pgid)
    ctx.save()


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
                                     prompt=step["prompt"], analysis=method.step_brief(ctx.plan), deps=dep_txt + note, acceptance=step["acceptance"] or "none")
    base_prompt = bridge.recall(f"{step['title']} {step['prompt'][:200]}", root=ctx.plan.get("repo")) + base_prompt
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
        started = []  # agent process groups this call starts: dropped from state once the call returns
        o = agents.Opts(str(wt), agents.WRITE_TOOLS, cap, int(os.environ.get("FLOW_TIMEOUT", "600")), on_group=lambda g: _track_group(ctx, ev, g, started))
        res, cost, tok = agents.call(ctx.code_chain, prompt, o, ev["api"])
        with ctx.slock:
            ev["agent_pgids"] = [g for g in ev.get("agent_pgids", []) if g not in started]
        ctx.add_cost(cost, tok, sid, f"step:{sid}", res.backend)
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
        g = gate.run_gates(str(wt), allowed=step["files"])
        acc = accept(step, str(wt))
        ev["attempts"].append({"n": n, "gates": _gate_line(g), "gates_ok": g["ok"], "acceptance": acc})
        ctx.save()
        if g["ok"] and acc["ok"]:
            passed = True
            journal.append(ctx.pid, sid, {"n": n, "status": "passed", "backend": res.backend, "files": _touched(str(wt)), "summary": summary[:300], "reflection": reflection})
            break
        fb = f"Gates: {_gate_line(g)}{_gate_details(g)}\nAcceptance `{acc['cmd']}` rc={acc.get('rc')}:\n{acc.get('tail', '')[-500:]}"
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
    _nomode()
    d, plan, state = load(pid)
    if state.get("plan_hash") and _hash(plan, state.get("dirty_approved", [])) != state["plan_hash"]:  # approval froze this plan: an edited plan.json must not run (or resume)
        state.update(status="failed", error="plan.json changed after approval: not running it. Re-plan and approve again", finished=engine.now())
        _atomic(d / "state.json", json.dumps(state, indent=2, ensure_ascii=False))
        raise PlanError(state["error"])
    lock = engine.try_lock(d / "run.lock")
    if not lock:
        return "already running"
    if state["status"] not in ("approved", "running"):
        raise PlanError(f"plan is {state['status']}")
    _kill_orphans(d, state)  # under the lock: groups a dead runner left behind are not ours to keep
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
        if "done" in sts:  # advisory passes: one failing never loses the plan result. Order: security, review (+simplify), code-verify, ship
            for key, fn in (("security", lambda: plan_audit(ctx, "security")), ("scout", lambda: scout_pass(ctx)), ("review", lambda: review_pass(ctx)), ("verify", lambda: plan_audit(ctx, "verify")), ("ship", lambda: ship_pass(ctx))):
                try:
                    state[key] = fn()
                except Exception as e:
                    state[key] = {"status": "not_run", "decision": "error", "error": f"{type(e).__name__}: {e}"[:200]}
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


def plan_audit(ctx, kind):
    """Steps security-hardening and code-verify over everything the plan changed (0 tokens). Same scans as the per-step gates, plus the soft findings."""
    wt, base = str(ctx.plan_wt), ctx.plan["base"]
    added = gate._added_lines(wt, base)
    if kind == "security":
        r = audit.hardening(added, gate._changed_names(wt, base))
    else:
        r = audit.verify(wt, added, {f for s in ctx.plan["steps"] for f in s["files"]}, gate._deleted(wt, base))
    return {"status": "ok", "hard": r["hard"][:15], "soft": r["soft"][:15], "n_hard": len(r["hard"]), "n_soft": len(r["soft"])}


def scout_pass(ctx):
    """The hands for the head: when code steps run on a cheaper model than the deciding one (Gemini parent: GPT-OSS), that model reads the merged result once
    and gathers quoted facts; review and the SHIP personas decide on those instead of each re-reading the repository. Every quote is checked against the file in
    code (method.parse_scout); the notes stay the scout's own reading. Off with FLOW_SCOUT=0, and when both chains start on the same model (nothing to save)."""
    if os.environ.get("FLOW_SCOUT", "1") != "1" or ctx.code_chain[:1] == ctx.chain[:1]:
        return {"status": "off"}
    with ctx.slock:
        cap = min(float(os.environ.get("FLOW_SCOUT_USD", "0.10")), ctx.free())
    if cap < 0.02:
        return {"status": "not_run", "error": "plan budget used up"}
    wt, base = str(ctx.plan_wt), ctx.plan["base"]
    changed = git(wt, "diff", "--name-only", f"{base}..HEAD").stdout.splitlines()
    res, cost, tok = agents.call(ctx.code_chain, method.scout_prompt(ctx.plan["goal"], (ctx.plan.get("analysis") or {}).get("spec", ""), changed), agents.Opts(wt, agents.READ_TOOLS, cap, 300), [])
    ctx.add_cost(cost, tok, role="scout", backend=res.backend)
    if not res.ok:
        return {"status": "not_run", "error": redact(res.error)[:200]}
    try:
        return method.parse_scout(extract_json(res.text), wt)
    except PlanError as e:
        return {"status": "not_run", "error": f"scout reply unusable: {e}"[:200]}


def _json_call(ctx, chain, prompt, opts, role):
    """One agent call that must answer with a JSON object. An unparsable reply (a model that spent its budget thinking, or answered in prose) gets one retry;
    the error keeps the tail of the last reply, so the failure can be read afterwards."""
    err = ""
    for attempt in (0, 1):
        res, cost, tok = agents.call(chain, prompt + ("\n\nYour previous reply was not a JSON object. Reply with the JSON object only, no prose, no code fence." if attempt else ""), opts, [])
        ctx.add_cost(cost, tok, role=role, backend=res.backend)
        if not res.ok:
            raise PlanError(redact(res.error)[:200])
        try:
            return extract_json(res.text)
        except PlanError as e:
            err = f"{e}; reply tail: {redact(res.text)[-300:]!r}"
    raise PlanError(err)


def _facts(ctx):
    return redact(method.scout_text((ctx.state.get("scout") or {}).get("facts") or []))


def review_pass(ctx):
    """Steps 10-11: one read-only agent call over the merged diff. Advisory: a failure or a finding never changes the plan status."""
    if os.environ.get("FLOW_REVIEW", "1") != "1":
        return {"status": "off"}
    with ctx.slock:
        cap = min(float(os.environ.get("FLOW_REVIEW_USD", "0.15")), ctx.free())
    if cap < 0.02:
        return {"status": "not_run", "error": "plan budget used up"}
    wt, base = str(ctx.plan_wt), ctx.plan["base"]
    changed = git(wt, "diff", "--name-only", f"{base}..HEAD").stdout.splitlines()
    diff = method.cap_diff(redact_shapes(git(wt, "diff", f"{base}..HEAD").stdout), int(os.environ.get("FLOW_REVIEW_BYTES", "30000")))
    try:
        data = _json_call(ctx, ctx.chain, method.review_prompt(ctx.plan["goal"], (ctx.plan.get("analysis") or {}).get("spec", ""), changed, diff, _facts(ctx)), agents.Opts(wt, agents.READ_TOOLS, cap, 300), "review")
        return method.parse_review(data, set(changed))
    except PlanError as e:
        return {"status": "not_run", "error": f"review reply unusable: {e}"[:500]}


def ship_pass(ctx):
    """Step 13 (the /ship command): three read-only personas in parallel over the merged diff, merged by code into GO / NO-GO with a rollback plan.
    Nothing is called when the plan already failed (that is a NO-GO on harness facts) or when the change is small (ship rule 5). Advisory: it never changes the plan status."""
    if os.environ.get("FLOW_SHIP", "1") != "1":
        return {"decision": "off"}
    wt, base, st = str(ctx.plan_wt), ctx.plan["base"], ctx.state
    changed = git(wt, "diff", "--name-only", f"{base}..HEAD").stdout.splitlines()
    nums = [l.split("\t")[:2] for l in git(wt, "diff", "--numstat", f"{base}..HEAD").stdout.splitlines()]
    lines = sum(int(a) + int(b) for a, b in nums if a.isdigit() and b.isdigit())
    full = git(wt, "diff", f"{base}..HEAD").stdout
    small = method.is_small(changed, lines, full)
    head = git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
    healthy = all(e["status"] == "done" for e in st["steps"].values()) and bool((st.get("integration") or {}).get("ok"))
    personas = {}
    if healthy and not small:
        diff = method.cap_diff(redact_shapes(full), int(os.environ.get("FLOW_SHIP_BYTES", "20000")))
        with ctx.slock:
            cap = min(float(os.environ.get("FLOW_SHIP_USD", "0.08")), ctx.free() / 3)
        spec, facts = (ctx.plan.get("analysis") or {}).get("spec", ""), _facts(ctx)

        def one(name):
            if cap < 0.02:
                return {"status": "not_run", "error": "plan budget used up"}
            try:
                data = _json_call(ctx, ctx.chain, method.persona_prompt(name, ctx.plan["goal"], spec, changed, diff, facts), agents.Opts(wt, agents.READ_TOOLS, cap, 300), f"ship:{name}")
                return method.parse_persona(name, data, set(changed))
            except Exception as e:  # one persona failing must not hide the others
                return {"status": "not_run", "error": f"{type(e).__name__}: {e}"[:500]}
        with ThreadPoolExecutor(len(method.PERSONAS)) as pool:
            personas = dict(zip(method.PERSONAS, pool.map(one, method.PERSONAS)))
    verdict = method.decide(ctx.plan, st, personas, changed, small, st.get("review"))
    trig = (personas.get("code-reviewer") or {}).get("rollback_triggers", [])
    return {**verdict, "small": small, "personas": personas, "rollback": method.rollback(ctx.pid, base, head, changed, trig)}


def _epoch(iso):
    return datetime.datetime.fromisoformat(iso).timestamp() if iso else 0.0


def _kill_orphans(d, st):
    """Kill the agent process groups a dead runner left behind, then forget them. Returns how many were killed.
    Call only while holding the plan's run lock: then no live runner can own these groups."""
    pgids = [g for ev in st["steps"].values() for g in ev.pop("agent_pgids", [])]
    if not pgids:
        return 0
    killed = sum(agents.kill_orphan_group(g) for g in pgids)
    _atomic(d / "state.json", json.dumps(st, indent=2, ensure_ascii=False))
    return killed


def _resume_if_dead(d, st, stale):
    """Restart the runner of a plan that has none: status running or approved, run lock free, heartbeat older than `stale` seconds
    (an approved plan with no heartbeat counts from its approval). Returns what was done, or None when the plan is alive or finished."""
    if st["status"] not in ("running", "approved"):
        return None
    last = float(st.get("heartbeat") or 0) or _epoch(st.get("approved"))
    if time.time() - last <= stale:
        return None
    lk = engine.try_lock(d / "run.lock")
    if not lk:
        return None  # a live runner holds the lock
    try:
        killed = _kill_orphans(d, st)
    finally:
        lk.close()
    spawn(st["id"])
    return f"resumed {st['id']} ({st['status']}, silent for {round(time.time() - last)}s)" + (f"; killed {killed} orphan agent group(s)" if killed else "")


def recover():
    """Resume every plan whose runner died, without the listener. Returns one line per plan acted on (and per failed check)."""
    stale = float(os.environ.get("FLOW_STALE", "120"))
    out = []
    for sf in sorted(plans_dir().glob("*/state.json")):
        try:
            msg = _resume_if_dead(sf.parent, json.loads(sf.read_text(encoding="utf-8")), stale)
        except Exception as e:
            msg = f"recover check failed for {sf.parent.name}: {type(e).__name__}: {e}"
        if msg:
            out.append(msg)
    return out


def resume_stale():
    """Listener check: the same as recover(), printed to the listener log."""
    for line in recover():
        print(line, flush=True)


def report(pid):
    """Write report.md/json: per step what was done, which files, why, and the evidence the harness itself collected."""
    d, plan, st = load(pid)
    steps = {s["id"]: s for s in plan["steps"]}
    L = [f"# Báo cáo kế hoạch {pid}: {st['status'].upper()}", "", f"- Mục tiêu: {plan['goal']}", f"- Branch để review: `flow/{pid}` (base `{plan['base'][:10]}`)",
         f"- Model/backend: {st.get('parent_model') or 'default'} | chain thực tế: {sorted({v.get('backend', '') for v in st['steps'].values() if v.get('backend')})}",
         f"- Chi phí: ${st.get('cost_usd', 0) + st.get('plan_cost_usd', 0):.4f} | thời gian: {st.get('duration_s', '?')}s | plan hash `{st.get('plan_hash', '')}`", ""]
    pt = [e for e in st.get("plan_trace", []) if e["action"] != "done"]
    L += [f"- Lập kế hoạch bởi: {[e['backend'] for e in st.get('plan_trace', []) if e['action'] == 'done']}"] + [f"  - API {e['backend']} lần {e['attempt']}: {e['error'][:120]} → {e['action']}" for e in pt]
    L += ["", "## Làm rõ yêu cầu (phỏng vấn)", ""] + interview.report_md(plan.get("brief"))
    L += ["", "## Phân tích (nghiên cứu, nguyên nhân gốc, cách làm, cách sửa, phạm vi ảnh hưởng, spec)", ""] + method.report_md(plan.get("analysis"))
    L += ["", "## Độ phủ 16 bước", "", "| # | Bước | Kết quả | Bằng chứng |", "|---|---|---|---|"] + [f"| {n} | {name} | {s} | {ev} |" for n, name, s, ev in method.coverage(plan, st)]
    L += ["", "| Bước | Trạng thái | File | Lần thử | Retry/fallback API |", "|---|---|---|---|---|"]
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
    L += method.audit_md("Security hardening (quét mẫu code nguy hiểm trên toàn bộ diff)", st.get("security"))
    rv = st.get("review") or {}
    if rv.get("status") == "ok":
        L += ["", "## Review và tinh gọn (agent đọc diff đã merge; chỉ để tham khảo, không tự sửa)"]
        L += [f"- [{r['severity']}] `{r['file']}:{r['line']}` {r['problem']} → {r['fix']}" for r in rv["review"]] or ["- review: không có phát hiện"]
        L += [f"- tinh gọn: `{x['file']}` {x['what']}" for x in rv["simplify"]]
        L += [f"- edge case đã xét: {'; '.join(rv['edge_cases'])}"] if rv["edge_cases"] else []
        L += [f"- bỏ {rv['dropped']} phát hiện không trỏ vào file trong diff"] if rv.get("dropped") else []
    L += method.audit_md("Code verify (chống code ẩu trên toàn bộ diff)", st.get("verify"))
    L += [""] + method.ship_md(st.get("ship") or {})
    agg = {}
    for c in st.get("calls", []):  # who spent which tokens on which model
        a = agg.setdefault((c["role"].split(":")[0], c["backend"]), [0, 0, 0, 0, 0])
        for i, v in enumerate((1, c.get("input_tokens", 0), c.get("cache_read", 0), c.get("output_tokens", 0), c.get("thinking", 0))):
            a[i] += v
    if agg:
        L += ["", "## Token theo vai trò", "", "| Vai trò | Backend | Lượt | Input | Cache đọc | Output | Suy nghĩ |", "|---|---|---|---|---|---|---|"]
        L += [f"| {r} | {b} | {a[0]} | {a[1]} | {a[2]} | {a[3]} | {a[4]} |" for (r, b), a in agg.items()]
    L += ["", "## Cách review", f"- `git diff {plan['base'][:10]}..flow/{pid}` xem toàn bộ thay đổi", f"- `git merge flow/{pid}` khi đồng ý; `git worktree remove plans/{pid}/wt/_plan` để dọn"]
    (d / "report.md").write_text(redact("\n".join(L) + "\n"), encoding="utf-8")
    _atomic(d / "report.json", json.dumps(redact_obj({"plan": plan, "state": st}), indent=2, ensure_ascii=False))
    return d / "report.md"


def _next_action(pid, st, d):
    if st == "awaiting_approval":
        return f"flow-task approve {pid}  (or edit plans/{pid}/plan.json first)"
    if st in ("approved", "running"):
        return f"wait; check with flow-task status {pid}"
    if st in ("failed", "partial"):
        return f"flow-task retry {pid}  (merged steps stay, failed ones run again)"
    if st == "done":
        return f"review flow/{pid}; report: {d / 'report.md'} (ship: see its 'Quyết định SHIP')"
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
