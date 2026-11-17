import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from flow_engine import agents, bench


def _fake(chain, prompt, o, trace=None, sleep=None):
    o.events.append({'step': 0, 'tool': 'Edit', 'file': 'flow_engine/agents.py'})
    trace.append({'backend': 'fake', 'ok': True})
    return agents.Result(True, 'x', 0.01, {'input_tokens': 5}), 0.01, {'input_tokens': 5}


@pytest.fixture
def bench_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FLOW_BENCH_UNSANDBOXED', '1')
    monkeypatch.setattr(bench.agents, 'call', _fake)
    for name, body in (('t1', 'exit 0'), ('t2', 'exit 1')):
        d = tmp_path / 'harness' / 'bench' / name
        d.mkdir(parents=True)
        (d / 'task.md').write_text('do it', encoding='utf-8')
        (d / 'accept.sh').write_text(body, encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)
    return tmp_path


def test_run_writes_results_and_trace(bench_dir, capsys):
    assert bench.cmd_run([]) == 0
    res = json.loads((bench_dir / 'harness' / 'runs' / '1' / 'results.json').read_text(encoding='utf-8'))
    assert res['run'] == 1
    assert res['complete'] is True
    assert res['tasks']['t1']['reward'] == 1
    assert res['tasks']['t2']['reward'] == 0
    lines = [json.loads(x) for x in (bench_dir / 'harness' / 'runs' / '1' / 'trace' / 't1.jsonl').read_text(encoding='utf-8').splitlines()]
    kinds = [x['kind'] for x in lines]
    assert 'tool' in kinds and 'accept' in kinds
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: flow-task bench compare'


def test_second_run_writes_next_number(bench_dir):
    bench.cmd_run([])
    bench.cmd_run([])
    assert (bench_dir / 'harness' / 'runs' / '2' / 'results.json').is_file()


def test_no_tasks_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FLOW_BENCH_UNSANDBOXED', '1')
    with pytest.raises(bench.BenchError):
        bench.cmd_run([])


def test_run_refused_without_sandbox(bench_dir, monkeypatch):
    monkeypatch.delenv('FLOW_BENCH_UNSANDBOXED', raising=False)
    monkeypatch.setattr(bench.sandbox, 'available', lambda: None)
    with pytest.raises(bench.BenchError):
        bench.cmd_run([])
    assert not (bench_dir / 'harness' / 'runs').exists()


def test_run_task_refused_without_sandbox(bench_dir, monkeypatch):
    monkeypatch.delenv('FLOW_BENCH_UNSANDBOXED', raising=False)
    monkeypatch.setattr(bench.sandbox, 'available', lambda: 'none')

    def no_copy(*a, **k):
        raise AssertionError('run_task copied before the sandbox check')

    monkeypatch.setattr(bench.tempfile, 'mkdtemp', no_copy)
    with pytest.raises(bench.BenchError, match='bwrap'):
        bench.run_task(bench.BENCH / 't1', 1)


def test_run_allowed_with_bwrap(bench_dir, monkeypatch):
    monkeypatch.delenv('FLOW_BENCH_UNSANDBOXED', raising=False)
    monkeypatch.setattr(bench.sandbox, 'available', lambda: 'bwrap')
    assert bench.cmd_run([]) == 0


def test_failed_call_still_writes_results(bench_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('boom')

    monkeypatch.setattr(bench.agents, 'call', boom)
    with pytest.raises(RuntimeError):
        bench.cmd_run([])
    res = json.loads((bench_dir / 'harness' / 'runs' / '1' / 'results.json').read_text(encoding='utf-8'))
    assert res['complete'] is False
    assert res['tasks'] == {}


def test_copy_takes_tracked_files_only(bench_dir, monkeypatch):
    (bench_dir / 'flow_engine').mkdir(exist_ok=True)
    (bench_dir / 'flow_engine' / 'gate.py').write_text('x', encoding='utf-8')
    (bench_dir / '.env').write_text('SECRET=1', encoding='utf-8')
    (bench_dir / 'key.pem').write_text('k', encoding='utf-8')
    (bench_dir / 'untracked.txt').write_text('u', encoding='utf-8')
    os.symlink(bench_dir / 'flow_engine' / 'gate.py', bench_dir / 'link.py')
    subprocess.run(['git', 'add', 'flow_engine', '.env', 'key.pem', 'link.py'], cwd=bench_dir, check=True)

    seen = []

    def capture(chain, prompt, o, trace=None, sleep=None):
        seen.extend(str(p.relative_to(o.workdir)) for p in Path(o.workdir).rglob('*') if p.is_file() or p.is_symlink())
        trace.append({'backend': 'fake', 'ok': True})
        return agents.Result(True, 'x', 0.0, {}), 0.0, {}

    monkeypatch.setattr(bench.agents, 'call', capture)
    bench.run_task(bench.BENCH / 't1', 1)
    assert 'flow_engine/gate.py' in seen
    assert '.env' not in seen
    assert 'key.pem' not in seen
    assert 'untracked.txt' not in seen
    assert 'link.py' not in seen


def test_absolute_event_path_is_relative(bench_dir, monkeypatch):
    def abs_fake(chain, prompt, o, trace=None, sleep=None):
        o.events.append({'step': 1, 'tool': 'Edit', 'file': str(Path(o.workdir) / 'flow_engine' / 'gate.py')})
        trace.append({'backend': 'fake', 'ok': True})
        return agents.Result(True, 'x', 0.01, {'input_tokens': 5}), 0.01, {'input_tokens': 5}

    monkeypatch.setattr(bench.agents, 'call', abs_fake)
    bench.cmd_run([])
    lines = [json.loads(x) for x in (bench_dir / 'harness' / 'runs' / '1' / 'trace' / 't1.jsonl').read_text(encoding='utf-8').splitlines()]
    tool = next(x for x in lines if x['kind'] == 'tool')
    assert tool['file'] == 'flow_engine/gate.py'


def test_class_of_maps_known_and_unknown_paths():
    assert bench.class_of('flow_engine/gate.py') == 'gates'
    assert bench.class_of('flow_engine/./agents.py') == 'backends'
    assert bench.class_of('flow_engine/unknown.py') is None


def _ok_call(chain, prompt, o, trace=None, sleep=None):
    trace.append({'backend': 'fake', 'ok': True})
    return agents.Result(ok=True, text='x', backend='x'), 0.0, {}


@pytest.fixture
def accept_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test-secret')
    monkeypatch.setenv('FLOW_BENCH_UNSANDBOXED', '1')
    monkeypatch.setattr(bench.sandbox, 'available', lambda: None)
    monkeypatch.setattr(bench.agents, 'call', _ok_call)
    monkeypatch.setattr(bench, '_copy_tracked', lambda repo: repo.mkdir(parents=True))
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(bench.tempfile, 'mkdtemp', lambda prefix='': str(scratch))
    return tmp_path


def _task(root, body):
    d = root / 'harness' / 'bench' / 'envtask'
    d.mkdir(parents=True)
    (d / 'task.md').write_text('do it', encoding='utf-8')
    (d / 'accept.sh').write_text(body, encoding='utf-8')
    return d


def test_accept_sh_does_not_inherit_api_key(accept_env):
    task = _task(accept_env, 'echo "KEY=${ANTHROPIC_API_KEY:-unset}"\nexit 0\n')
    res = bench.run_task(task, 1)
    assert res['reward'] == 1
    assert 'KEY=unset' in res['out']
    assert 'sk-test-secret' not in res['out']


def test_accept_sh_exit_one_is_reward_zero(accept_env):
    task = _task(accept_env, 'exit 1\n')
    assert bench.run_task(task, 1)['reward'] == 0


def test_accept_sh_timeout_is_reward_zero(accept_env, monkeypatch):
    def timed_out(*a, **k):
        raise subprocess.TimeoutExpired(cmd='x', timeout=300)

    monkeypatch.setattr(bench.subprocess, 'run', timed_out)
    task = _task(accept_env, 'exit 0\n')
    res = bench.run_task(task, 1)
    assert res['reward'] == 0
    assert res['out'] == 'accept.sh timeout'


def test_accept_sh_runs_under_bwrap_when_available(accept_env, monkeypatch):
    monkeypatch.setattr(bench.sandbox, 'available', lambda: 'bwrap')
    argvs = []

    def record(argv, **k):
        argvs.append(argv)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(bench.subprocess, 'run', record)
    task = _task(accept_env, 'exit 0\n')
    assert bench.run_task(task, 1)['reward'] == 1
    assert argvs[0][0] == 'bwrap'
    assert argvs[0][-2:] == ['bash', str((task / 'accept.sh').resolve())]


def test_non_str_event_file_does_not_raise(tmp_path):
    events = [{'file': 5, 'step': 1}]
    bench._relative_events(events, tmp_path)
    assert events[0]['file'] == ''


def test_tracked_env_example_is_copied_but_env_is_not(bench_dir):
    (bench_dir / '.env.example').write_text('KEY=\n', encoding='utf-8')
    (bench_dir / '.env').write_text('SECRET=1', encoding='utf-8')
    subprocess.run(['git', 'add', '.env.example', '.env', '-f'], cwd=bench_dir, check=True)
    repo = bench_dir / 'copy'
    bench._copy_tracked(repo)
    assert (repo / '.env.example').read_text(encoding='utf-8') == 'KEY=\n'
    assert not (repo / '.env').exists()


def test_run_task_records_turns_and_denied(bench_dir, monkeypatch):
    def turns_fake(chain, prompt, o, trace=None, sleep=None):
        o.events.append({'turn': 1, 'tool': 'Read'})
        o.events.append({'turn': 3, 'tool': 'Bash', 'denied': True})
        o.events.append({'turn': 2, 'tool': 'Bash', 'denied': True})
        o.events.append({'turn': 2, 'tool': 'Grep', 'denied': False})
        trace.append({'backend': 'fake', 'ok': True})
        return agents.Result(True, 'x', 0.0, {}), 0.0, {}

    monkeypatch.setattr(bench.agents, 'call', turns_fake)
    res = bench.run_task(bench.BENCH / 't1', 1)
    assert res['turns'] == 3
    assert res['denied'] == 2


def test_run_task_without_events_has_zero_turns(bench_dir):
    res = bench.run_task(bench.BENCH / 't1', 1)
    assert res['turns'] == 0
    assert res['denied'] == 0


def test_max_turn_ignores_non_int_turns():
    events = [{'turn': True}, {'turn': '4'}, {'turn': 2.0}, {'turn': 1}]
    assert bench._max_turn(events) == 1


def test_results_json_keeps_turns_and_denied(bench_dir, monkeypatch):
    def turns_fake(chain, prompt, o, trace=None, sleep=None):
        o.events.append({'turn': 2, 'tool': 'Bash', 'denied': True})
        trace.append({'backend': 'fake', 'ok': True})
        return agents.Result(True, 'x', 0.0, {}), 0.0, {}

    monkeypatch.setattr(bench.agents, 'call', turns_fake)
    bench.cmd_run([])
    res = json.loads((bench_dir / 'harness' / 'runs' / '1' / 'results.json').read_text(encoding='utf-8'))
    assert res['tasks']['t1']['turns'] == 2
    assert res['tasks']['t1']['denied'] == 1
    assert 'events' not in res['tasks']['t1']
