import json
import os
import subprocess
from pathlib import Path

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
