'''Bench: run harness/bench/<task> tasks against the agent chain and record a numbered run.

Reward is the exit code of each task's accept.sh (0 = pass). Output goes to harness/runs/<N>/.
'''
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from flow_engine import agents, sandbox, secrets

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
    'change': 'NEXT: flow-task bench change --file <F> --evidence <E> --root <R> --fix <X> --predict <T|none> [--risk <T>]',
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


def _is_complete(path):
    """A results.json counts as complete unless it says "complete": false. A file that does not parse counts as complete, so _rewards raises for it."""
    if not path.is_file():
        return False
    try:
        parsed = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return True
    return not (isinstance(parsed, dict) and parsed.get('complete') is False)


def _numbered_runs(complete_only=False):
    if not RUNS.is_dir():
        return []
    return [int(d.name) for d in RUNS.iterdir() if d.is_dir() and re.fullmatch(r'\d+', d.name) and (not complete_only or _is_complete(d / 'results.json'))]


def next_run():
    return 1 + max(_numbered_runs(), default=0)


def latest_run():
    nums = _numbered_runs(complete_only=True)
    return max(nums) if nums else None


def chain():
    return [b.strip() for b in os.environ.get('FLOW_BENCH_BACKENDS', 'claude:haiku').split(',') if b.strip()]


def _require_sandbox():
    if sandbox.available() != 'bwrap' and os.environ.get('FLOW_BENCH_UNSANDBOXED') != '1':
        raise BenchError('bench run needs the bwrap sandbox (bubblewrap); set FLOW_BENCH_UNSANDBOXED=1 to run without it')


def run_task(task_dir, n):
    _require_sandbox()
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
        accept_path = str((task_dir / 'accept.sh').resolve())
        argv = sandbox.wrap(['bash', accept_path], str(repo)) if sandbox.available() == 'bwrap' else ['bash', accept_path]
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': tmp, 'LANG': 'C.UTF-8'}
        try:
            p = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, timeout=300, env=env)
            rc = p.returncode
            out = (p.stdout or '') + (p.stderr or '')
        except subprocess.TimeoutExpired:
            rc = -1
            out = 'accept.sh timeout'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {'reward': 1 if rc == 0 else 0, 'accept_exit': rc, 'cost_usd': round(cost, 6), 'tokens': tok, 'ok': res.ok,
            'error': (res.error or '')[:200], 'backend': res.backend, 'out': out[-2000:], 'trace': trace, 'events': events,
            'turns': _max_turn(events), 'denied': sum(1 for ev in events if ev.get('denied') is True)}


def _max_turn(events):
    turns = [ev['turn'] for ev in events if _is_int(ev.get('turn'))]
    return max(turns, default=0)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def cmd_run(rest):
    _require_sandbox()
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


CHANGE_USAGE = 'usage: flow-task bench change --file F [--file G] --evidence E --root R --fix X --predict T1,T2|none [--risk T1,T2]'
CHANGE_FLAGS = ('--file', '--evidence', '--root', '--fix', '--predict', '--risk')


def _change_args(rest):
    opts = {'--file': [], '--evidence': '', '--root': '', '--fix': '', '--predict': '', '--risk': ''}
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
    if Path(path).is_symlink():
        raise BenchError(f'refusing a symlink: {path}')
    if not Path(path).is_file():
        raise BenchError(f'no such file: {path}')
    try:
        Path(path).resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        raise BenchError(f'refusing a path outside the repo: {path}')
    return path


_SECRET_NAMES = ('.npmrc', '.netrc', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519')
_SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx')


def _secret_name(name):
    if name in secrets.SECRET_FILE_SAFE:
        return False
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
        if not isinstance(ev.get('file', ''), str):
            ev['file'] = ''
            continue
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
    raw_predict = fields['--predict']
    predict = [] if raw_predict == 'none' else [p.strip() for p in raw_predict.split(',') if p.strip()]
    if not predict and raw_predict != 'none':
        raise BenchError('missing --predict')
    risk = [r.strip() for r in opts['--risk'].split(',') if r.strip()]
    known = {d.name for d in tasks()}
    for name in predict + risk:
        if name not in known:
            raise BenchError(f'unknown task: {name}')
    n = next_run()
    if (MANIFEST / f'{n}.json').exists():
        raise BenchError(f'manifest for run {n} already exists: one change per run, run the bench first')
    snapshot = {path: Path(path).read_text(encoding='utf-8') for path in files}
    manifest = {'run': n, 'files': files, 'evidence': fields['--evidence'], 'root': fields['--root'],
                'fix': fields['--fix'], 'predict': predict, 'risk': risk, 'snapshot': snapshot}
    MANIFEST.mkdir(parents=True, exist_ok=True)
    out = MANIFEST / f'{n}.json'
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'manifest written: {out}')
    print('NEXT: edit the files, then: flow-task bench run')
    return 0


def _load_results(n):
    try:
        parsed = json.loads((RUNS / str(n) / 'results.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise BenchError(f'bad results file for run {n}: not valid JSON')
    if not isinstance(parsed, dict) or not isinstance(parsed.get('tasks'), dict):
        raise BenchError(f'bad results file for run {n}: no tasks object')
    for name, item in parsed['tasks'].items():
        if not isinstance(item, dict):
            raise BenchError(f'bad results file for run {n}: bad task entry {name}')
        try:
            int(item.get('reward', 0))
        except (TypeError, ValueError, OverflowError):
            raise BenchError(f'bad results file for run {n}: bad task entry {name}')
    return parsed


def _rewards(n):
    data = _load_results(n)
    return {name: int(item.get('reward', 0)) for name, item in data['tasks'].items()}


def _check_manifest(manifest, n):
    bad = BenchError(f'bad manifest for run {n}')
    if not isinstance(manifest, dict):
        raise bad
    predict = manifest.get('predict')
    if not isinstance(predict, list) or not all(isinstance(p, str) for p in predict):
        raise bad
    snapshot = manifest.get('snapshot')
    if not isinstance(snapshot, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in snapshot.items()):
        raise bad
    risk = manifest.get('risk', [])
    if not isinstance(risk, list) or not all(isinstance(r, str) for r in risk):
        raise bad


def _total(tasks, key):
    total = 0.0
    for name, item in tasks.items():
        value = item.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise BenchError(f'bad results file: {key} of {name} is not a number')
        total += value
    return total


def _turn_total(tasks):
    """Sum of the per-task turns, or None when any task has no int turns (results from before turns were recorded)."""
    values = [item.get('turns') for item in tasks.values()]
    if not all(_is_int(v) for v in values):
        return None
    return sum(values)


def _warn_incomplete(n):
    m = max(_numbered_runs(), default=0)
    if m > n:
        print(f'warning: run {m} is incomplete, using run {n}')


def cmd_compare(rest):
    n = latest_run()
    if n is None:
        raise BenchError('no run yet: flow-task bench run')
    _warn_incomplete(n)
    cur_tasks = _load_results(n)['tasks']
    cur = _rewards(n)
    if n - 1 < 1 or not _is_complete(RUNS / str(n - 1) / 'results.json'):
        print('no previous run to compare with (run N is the first)')
        print('NEXT: flow-task bench analyze')
        return 0
    prev_tasks = _load_results(n - 1)['tasks']
    prev = _rewards(n - 1)
    flips = []
    for name in sorted(set(prev) | set(cur)):
        a = prev.get(name, 0)
        b = cur.get(name, 0)
        if a != b:
            flips.append((name, a, b))
            print(f'{name}: {a}->{b}')
    cost_a, cost_b = _total(prev_tasks, 'cost_usd'), _total(cur_tasks, 'cost_usd')
    pct = f' ({(cost_b - cost_a) / cost_a * 100:+.1f}%)' if cost_a else ''
    print(f'cost {cost_a:.4f} -> {cost_b:.4f} USD{pct}')
    turns_a, turns_b = _turn_total(prev_tasks), _turn_total(cur_tasks)
    turns = None if turns_a is None or turns_b is None else [turns_a, turns_b]
    print('turns n/a' if turns is None else f'turns {turns_a} -> {turns_b}')
    improvements = sum(1 for _, a, b in flips if a == 0 and b == 1)
    regression_names = [name for name, a, b in flips if a == 1 and b == 0]
    regressions = len(regression_names)
    manifest_file = MANIFEST / f'{n}.json'
    manifest = None
    if manifest_file.is_file():
        try:
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise BenchError(f'bad manifest for run {n}')
        _check_manifest(manifest, n)
    risk = set(manifest.get('risk', [])) if manifest is not None else set()
    expected = [name for name in regression_names if name in risk]
    unexpected = [name for name in regression_names if name not in risk]
    if manifest is not None and regressions:
        print(f'regressions: {len(expected)} expected (in --risk), {len(unexpected)} unexpected')
        if expected:
            print(f'  expected: {", ".join(expected)}')
        if unexpected:
            print(f'  unexpected: {", ".join(unexpected)}')
    predicted_flip = None
    reason = ''
    restore = False
    if manifest is not None:
        predict = manifest['predict']
        predicted_flip = sum(1 for name in predict if prev.get(name, 0) == 0 and cur.get(name, 0) == 1)
        if regressions > improvements:
            reason = f'regressions {regressions} > flips {improvements}'
        elif predict and predicted_flip == 0:
            reason = f'zero predicted flips (predicted: {", ".join(predict)})'
        restore = bool(reason)
    restored = []
    if restore:
        targets = [(_safe_path(path), text) for path, text in manifest['snapshot'].items()]
        cwd = Path.cwd().resolve()
        for path, _ in targets:
            for candidate in [Path(path).absolute()] + list(Path(path).absolute().parents):
                try:
                    candidate.relative_to(cwd)
                except ValueError:
                    continue
                if candidate.is_symlink():
                    raise BenchError(f'refusing to restore through a symlink: {path}')
            try:
                Path(path).resolve().relative_to(cwd)
            except ValueError:
                raise BenchError(f'refusing to restore outside the repo: {path}')
        for path, text in targets:
            Path(path).write_text(text, encoding='utf-8')
            restored.append(path)
    eval_data = {'run': n, 'improvements': improvements, 'regressions': regressions, 'predicted_flip': predicted_flip,
                 'restored': restored, 'reason': reason, 'cost': [round(cost_a, 6), round(cost_b, 6)], 'turns': turns,
                 'regressions_expected': expected, 'regressions_unexpected': unexpected}
    (RUNS / str(n) / 'eval.json').write_text(json.dumps(eval_data, indent=2, ensure_ascii=False), encoding='utf-8')
    if restored:
        print(f'restored {", ".join(restored)} to run {n - 1} state: {reason}')
    elif manifest is not None:
        print(f'kept run {n} changes')
    else:
        print(f'no manifest for run {n}: nothing to keep or restore')
    print('NEXT: flow-task bench analyze')
    return 0


def _tool_events(trace_path):
    lines = []
    if trace_path.is_file():
        for raw in trace_path.read_text(encoding='utf-8').splitlines():
            if raw.strip():
                try:
                    item = json.loads(raw)
                except ValueError:
                    raise BenchError(f'bad trace line in {trace_path.name}')
                if not isinstance(item, dict):
                    continue
                if item.get('kind') == 'tool':
                    lines.append(item)
    return lines


def _denied(trace_path):
    return sum(1 for item in _tool_events(trace_path) if item.get('denied') is True)


def _failing_step_class(trace_path):
    lines = _tool_events(trace_path)
    if not lines:
        return 0, 'unclassified'
    def turn_of(item):
        return item.get('turn', item.get('step', 0))

    turn = turn_of(lines[-1])
    last = [line for line in lines if turn_of(line) == turn]
    if any(line.get('denied') is True for line in last):
        return turn, 'backends'
    touched = [line.get('file', '') for line in last if line.get('file')]
    cls = next((c for c in (class_of(f) for f in touched) if c is not None), 'unclassified')
    return turn, cls


def cmd_analyze(rest):
    n = latest_run()
    if n is None:
        raise BenchError('no run yet: flow-task bench run')
    _warn_incomplete(n)
    results = _load_results(n)
    failing = sorted(name for name, item in results['tasks'].items() if int(item.get('reward', 0)) == 0)
    if not failing:
        print(f'no failing tasks in run {n}: the bench is saturated, add a task that fails under harness/bench/')
        print('NEXT: flow-task bench run')
        return 0
    groups = {}
    for task in failing:
        trace_path = RUNS / str(n) / 'trace' / f'{task}.jsonl'
        turn, cls = _failing_step_class(trace_path)
        denied = _denied(trace_path)
        label = f'[turn {turn}, {denied} denied]' if denied else f'[turn {turn}]'
        groups.setdefault(cls, []).append((task, label))
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
        print('usage: flow-task bench run | compare | analyze | change --file F --evidence E --root R --fix X --predict T1,T2|none [--risk T1,T2]')
        print('NEXT: flow-task bench run')
        return 1
    fn = {'run': cmd_run, 'compare': cmd_compare, 'analyze': cmd_analyze, 'change': cmd_change}[sub]
    try:
        return fn(rest[1:])
    except (BenchError, OSError, ValueError, KeyError, AttributeError, TypeError, OverflowError) as e:
        print('bench error: ' + str(e), file=sys.stderr)
        print(NEXT_ON_ERROR[sub])
        return 1
