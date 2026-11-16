'''Bench: run harness/bench/<task> tasks against the agent chain and record a numbered run.

Reward is the exit code of each task's accept.sh (0 = pass). Output goes to harness/runs/<N>/.
'''
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from flow_engine import agents, sandbox

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


SUBCOMMANDS = ('run', 'compare', 'analyze', 'change')
NEXT_ON_ERROR = {
    'run': 'NEXT: flow-task bench run',
    'compare': 'NEXT: flow-task bench compare',
    'analyze': 'NEXT: flow-task bench analyze',
    'change': 'NEXT: flow-task bench change --file <F> --evidence <E> --root <R> --fix <X> --predict <T>',
}


def class_of(path):
    p = Path(path).as_posix()
    for key, files in COMPONENTS.items():
        for f in files:
            if p == f or p.endswith('/' + f):
                return key
    return None


def _safe_path(raw):
    path = Path(raw[2:] if raw.startswith('./') else raw).as_posix()
    if Path(path).is_absolute() or '..' in Path(path).parts or not any(path in files for files in COMPONENTS.values()):
        raise BenchError(f'outside the 7 component dirs: {path}')
    return path


def tasks():
    found = sorted(d for d in BENCH.glob('*') if d.is_dir() and (d / 'task.md').is_file() and (d / 'accept.sh').is_file()) if BENCH.is_dir() else []
    if not found:
        raise BenchError('no bench tasks in harness/bench')
    return found


def _numbered_runs(complete_only=False):
    if not RUNS.is_dir():
        return []
    return [int(d.name) for d in RUNS.iterdir() if d.is_dir() and re.fullmatch(r'\d+', d.name) and (not complete_only or (d / 'results.json').is_file())]


def next_run():
    return 1 + max(_numbered_runs(), default=0)


def latest_run():
    nums = _numbered_runs(complete_only=True)
    return max(nums) if nums else None


def chain():
    return [b.strip() for b in os.environ.get('FLOW_BENCH_BACKENDS', 'claude:haiku').split(',') if b.strip()]


def run_task(task_dir, n):
    tmp = tempfile.mkdtemp(prefix='bench-')
    repo = Path(tmp) / 'repo'
    try:
        _copy_tracked(repo)
        events = []
        trace = []
        res, cost, tok = agents.call(chain(), (task_dir / 'task.md').read_text(encoding='utf-8'),
                                     agents.Opts(workdir=str(repo), tools='Read,Edit,Write,Bash,Glob,Grep',
                                                 budget=float(os.environ.get('FLOW_BENCH_USD', '0.50')), timeout=600, events=events),
                                     trace)
        _relative_events(events, repo)
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
    if sandbox.available() != 'bwrap' and os.environ.get('FLOW_BENCH_UNSANDBOXED') != '1':
        raise BenchError('bench run needs the bwrap sandbox (bubblewrap); set FLOW_BENCH_UNSANDBOXED=1 to run without it')
    todo = tasks()
    n = next_run()
    run_dir = RUNS / str(n)
    trace_dir = run_dir / 'trace'
    trace_dir.mkdir(parents=True)
    results = {}
    try:
        for d in todo:
            r = run_task(d, n)
            lines = [{'kind': 'attempt', **item} for item in r['trace']]
            lines += [{'kind': 'tool', **ev} for ev in r['events']]
            lines.append({'kind': 'accept', 'exit': r['accept_exit'], 'out': r['out']})
            (trace_dir / (d.name + '.jsonl')).write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in lines), encoding='utf-8')
            results[d.name] = {k: v for k, v in r.items() if k not in ('trace', 'events', 'out')}
            print(f"task {d.name} reward={r['reward']} cost={r['cost_usd']} accept_exit={r['accept_exit']}")
    finally:
        complete = len(results) == len(todo)
        (run_dir / 'results.json').write_text(json.dumps({'run': n, 'complete': complete, 'tasks': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('NEXT: flow-task bench compare')
    return 0


CHANGE_USAGE = 'usage: flow-task bench change --file F [--file G] --evidence E --root R --fix X --predict T1,T2'
CHANGE_FLAGS = ('--file', '--evidence', '--root', '--fix', '--predict')


def _change_args(rest):
    opts = {'--file': [], '--evidence': '', '--root': '', '--fix': '', '--predict': ''}
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag not in CHANGE_FLAGS or i + 1 >= len(rest):
            raise BenchError(CHANGE_USAGE)
        value = rest[i + 1]
        if flag == '--file':
            opts['--file'].append(value)
        else:
            opts[flag] = value
        i += 2
    return opts


def _check_file(raw):
    path = _safe_path(raw)
    if not Path(path).is_file():
        raise BenchError(f'no such file: {path}')
    return path


_SECRET_NAMES = ('.npmrc', '.netrc', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519')
_SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx')


def _secret_name(name):
    return name.startswith('.env') or name in _SECRET_NAMES or name.lower().endswith(_SECRET_SUFFIXES)


def _tracked_files():
    p = subprocess.run(['git', 'ls-files', '-z'], capture_output=True)
    if p.returncode != 0:
        raise BenchError('bench run needs a git repo: git ls-files failed')
    return [x for x in p.stdout.decode('utf-8', 'replace').split(chr(0)) if x]


def _copy_tracked(repo):
    repo.mkdir(parents=True, exist_ok=True)
    for rel in _tracked_files():
        if rel.startswith('harness/'):
            continue
        src = Path.cwd() / rel
        if src.is_symlink() or not src.is_file() or _secret_name(src.name):
            continue
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _relative_events(events, repo):
    for ev in events:
        f = ev.get('file') or ''
        if not f or not Path(f).is_absolute():
            continue
        for base in (repo, repo.resolve()):
            try:
                ev['file'] = Path(f).relative_to(base).as_posix()
                break
            except ValueError:
                continue


def cmd_change(rest):
    opts = _change_args(rest)
    fields = {}
    for name in ('--evidence', '--root', '--fix', '--predict'):
        value = opts[name].strip()
        if not value:
            raise BenchError(f'missing {name}')
        fields[name] = value
    if not 1 <= len(opts['--file']) <= 2:
        raise BenchError('change may touch 1 or 2 files')
    files = [_check_file(f) for f in opts['--file']]
    predict = [p.strip() for p in fields['--predict'].split(',') if p.strip()]
    if not predict:
        raise BenchError('missing --predict')
    known = {d.name for d in tasks()}
    for name in predict:
        if name not in known:
            raise BenchError(f'unknown task: {name}')
    n = next_run()
    if (MANIFEST / f'{n}.json').exists():
        raise BenchError(f'manifest for run {n} already exists: one change per run, run the bench first')
    snapshot = {path: Path(path).read_text(encoding='utf-8') for path in files}
    manifest = {'run': n, 'files': files, 'evidence': fields['--evidence'], 'root': fields['--root'],
                'fix': fields['--fix'], 'predict': predict, 'snapshot': snapshot}
    MANIFEST.mkdir(parents=True, exist_ok=True)
    out = MANIFEST / f'{n}.json'
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'manifest written: {out}')
    print('NEXT: flow-task bench run')
    return 0


def _rewards(n):
    data = json.loads((RUNS / str(n) / 'results.json').read_text(encoding='utf-8'))
    return {name: int(item.get('reward', 0)) for name, item in data['tasks'].items()}


def cmd_compare(rest):
    n = latest_run()
    if n is None:
        raise BenchError('no run yet: flow-task bench run')
    cur = _rewards(n)
    if n - 1 < 1 or not (RUNS / str(n - 1) / 'results.json').is_file():
        print('no previous run to compare with (run N is the first)')
        print('NEXT: flow-task bench analyze')
        return 0
    prev = _rewards(n - 1)
    flips = []
    for name in sorted(set(prev) | set(cur)):
        a = prev.get(name, 0)
        b = cur.get(name, 0)
        if a != b:
            flips.append((name, a, b))
            print(f'{name}: {a}->{b}')
    improvements = sum(1 for _, a, b in flips if a == 0 and b == 1)
    regressions = sum(1 for _, a, b in flips if a == 1 and b == 0)
    manifest_file = MANIFEST / f'{n}.json'
    manifest = json.loads(manifest_file.read_text(encoding='utf-8')) if manifest_file.is_file() else None
    predicted_flip = None
    reason = ''
    restore = False
    if manifest is not None:
        predict = manifest['predict']
        predicted_flip = sum(1 for name in predict if prev.get(name, 0) == 0 and cur.get(name, 0) == 1)
        if regressions > improvements:
            reason = f'regressions {regressions} > flips {improvements}'
        elif predicted_flip == 0:
            reason = f'zero predicted flips (predicted: {", ".join(predict)})'
        restore = bool(reason)
    restored = []
    if restore:
        targets = [(_safe_path(path), text) for path, text in manifest['snapshot'].items()]
        for path, text in targets:
            Path(path).write_text(text, encoding='utf-8')
            restored.append(path)
    eval_data = {'run': n, 'improvements': improvements, 'regressions': regressions, 'predicted_flip': predicted_flip,
                 'restored': restored, 'reason': reason}
    (RUNS / str(n) / 'eval.json').write_text(json.dumps(eval_data, indent=2, ensure_ascii=False), encoding='utf-8')
    if restored:
        print(f'restored {", ".join(restored)} to run {n - 1} state: {reason}')
    else:
        print('kept run N changes')
    print('NEXT: flow-task bench analyze')
    return 0


def _failing_step_class(trace_path):
    lines = []
    if trace_path.is_file():
        for raw in trace_path.read_text(encoding='utf-8').splitlines():
            if raw.strip():
                item = json.loads(raw)
                if item.get('kind') == 'tool':
                    lines.append(item)
    if not lines:
        return 0, 'unclassified'
    step = lines[-1].get('step', 0)
    touched = [line.get('file', '') for line in lines if line.get('step', 0) == step and line.get('file')]
    cls = next((c for c in (class_of(f) for f in touched) if c is not None), 'unclassified')
    return step, cls


def cmd_analyze(rest):
    n = latest_run()
    if n is None:
        raise BenchError('no run yet: flow-task bench run')
    results = json.loads((RUNS / str(n) / 'results.json').read_text(encoding='utf-8'))
    failing = sorted(name for name, item in results['tasks'].items() if int(item.get('reward', 0)) == 0)
    if not failing:
        print(f'no failing tasks in run {n}')
        print('NEXT: flow-task bench run')
        return 0
    groups = {}
    for task in failing:
        step, cls = _failing_step_class(RUNS / str(n) / 'trace' / f'{task}.jsonl')
        groups.setdefault(cls, []).append((task, f'[step {step}]'))
    order = sorted(groups, key=lambda c: (-len(groups[c]), c))
    for cls in order:
        items = ', '.join(f'{task} {key}' for task, key in groups[cls])
        print(f'{cls}: {len(groups[cls])} failing: {items}')
    top = order[0]
    top_file = COMPONENTS[top][0] if top in COMPONENTS else 'FILE'
    predict = ','.join(task for task, _ in groups[top])
    print(f'NEXT: flow-task bench change --file {top_file} --evidence <E> --root <R> --fix <X> --predict {predict}')
    return 0


def main(rest):
    sub = rest[0] if rest else ''
    if sub not in SUBCOMMANDS:
        print('usage: flow-task bench run | compare | analyze | change --file F --evidence E --root R --fix X --predict T')
        print('NEXT: flow-task bench run')
        return 1
    fn = {'run': cmd_run, 'compare': cmd_compare, 'analyze': cmd_analyze, 'change': cmd_change}[sub]
    try:
        return fn(rest[1:])
    except BenchError as e:
        print('bench error: ' + str(e), file=sys.stderr)
        print(NEXT_ON_ERROR[sub])
        return 1
    except (OSError, ValueError) as e:
        print('bench error: ' + str(e), file=sys.stderr)
        print(NEXT_ON_ERROR[sub])
        return 1
