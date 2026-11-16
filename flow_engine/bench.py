'''Bench: run harness/bench/<task> tasks against the agent chain and record a numbered run.

Reward is the exit code of each task's accept.sh (0 = pass). Output goes to harness/runs/<N>/.
'''
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from flow_engine import agents

BENCH = Path('harness') / 'bench'
RUNS = Path('harness') / 'runs'
MANIFEST = Path('harness') / 'manifest'

COMPONENTS = {
    'prompts': ['flow_engine/method.py'],
    'planning': ['flow_engine/plan.py', 'flow_engine/interview.py'],
    'gates': ['flow_engine/gate.py', 'flow_engine/audit.py', 'flow_engine/secrets.py', 'flow_engine/claims.py', 'flow_engine/claims_llm.py'],
    'backends': ['flow_engine/agents.py', 'flow_engine/api.py', 'flow_engine/apiclient.py', 'flow_engine/apiprice.py', 'flow_engine/sandbox.py'],
    'engine': ['flow_engine/engine.py', 'flow_engine/journal.py'],
    'state_io': ['flow_engine/bridge.py', 'flow_engine/resp.py', 'flow_engine/apilog.py'],
    'cli': ['flow_engine/cli.py', 'flow_engine/__main__.py', 'flow_engine/doctor_api.py', 'flow_engine/selftest.py'],
}


class BenchError(Exception):
    pass


def class_of(path):
    p = Path(path).as_posix()
    for key, files in COMPONENTS.items():
        if p in files:
            return key
    return None


def tasks():
    found = sorted(d for d in BENCH.glob('*') if d.is_dir() and (d / 'task.md').is_file() and (d / 'accept.sh').is_file()) if BENCH.is_dir() else []
    if not found:
        raise BenchError('no bench tasks in harness/bench')
    return found


def _numbered_runs():
    if not RUNS.is_dir():
        return []
    return [int(d.name) for d in RUNS.iterdir() if d.is_dir() and re.fullmatch(r'\d+', d.name)]


def next_run():
    return 1 + max(_numbered_runs(), default=0)


def latest_run():
    nums = _numbered_runs()
    return max(nums) if nums else None


def chain():
    return [b.strip() for b in os.environ.get('FLOW_BENCH_BACKENDS', 'claude:haiku').split(',') if b.strip()]


def run_task(task_dir, n):
    tmp = tempfile.mkdtemp(prefix='bench-')
    repo = Path(tmp) / 'repo'
    try:
        shutil.copytree(Path.cwd(), repo, ignore=shutil.ignore_patterns('.git', 'harness', '__pycache__'))
        events = []
        trace = []
        res, cost, tok = agents.call(chain(), (task_dir / 'task.md').read_text(encoding='utf-8'),
                                     agents.Opts(workdir=str(repo), tools='Read,Edit,Write,Bash,Glob,Grep',
                                                 budget=float(os.environ.get('FLOW_BENCH_USD', '0.50')), timeout=600, events=events),
                                     trace)
        try:
            p = subprocess.run(['bash', str((task_dir / 'accept.sh').resolve())], cwd=str(repo), capture_output=True, text=True, timeout=300)
            rc = p.returncode
            out = (p.stdout or '') + (p.stderr or '')
        except subprocess.TimeoutExpired:
            rc = -1
            out = 'accept.sh timeout'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {'reward': 1 if rc == 0 else 0, 'accept_exit': rc, 'cost_usd': round(cost, 6), 'tokens': tok, 'ok': res.ok,
            'error': (res.error or '')[:200], 'backend': res.backend, 'out': out[-2000:], 'trace': trace, 'events': events}


def cmd_run(rest):
    n = next_run()
    trace_dir = RUNS / str(n) / 'trace'
    trace_dir.mkdir(parents=True)
    results = {}
    for d in tasks():
        r = run_task(d, n)
        lines = [{'kind': 'attempt', **item} for item in r['trace']]
        lines += [{'kind': 'tool', **ev} for ev in r['events']]
        lines.append({'kind': 'accept', 'exit': r['accept_exit'], 'out': r['out']})
        (trace_dir / (d.name + '.jsonl')).write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in lines), encoding='utf-8')
        results[d.name] = {k: v for k, v in r.items() if k not in ('trace', 'events', 'out')}
        print(f"task {d.name} reward={r['reward']} cost={r['cost_usd']} accept_exit={r['accept_exit']}")
    (RUNS / str(n) / 'results.json').write_text(json.dumps({'run': n, 'tasks': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NEXT: flow-task bench compare')
    return 0
