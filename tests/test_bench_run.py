import json

import pytest

from flow_engine import agents, bench


def _fake(chain, prompt, o, trace=None, sleep=None):
    o.events.append({'step': 0, 'tool': 'Edit', 'file': 'flow_engine/agents.py'})
    trace.append({'backend': 'fake', 'ok': True})
    return agents.Result(True, 'x', 0.01, {'input_tokens': 5}), 0.01, {'input_tokens': 5}


@pytest.fixture
def bench_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bench.agents, 'call', _fake)
    for name, body in (('t1', 'exit 0'), ('t2', 'exit 1')):
        d = tmp_path / 'harness' / 'bench' / name
        d.mkdir(parents=True)
        (d / 'task.md').write_text('do it', encoding='utf-8')
        (d / 'accept.sh').write_text(body, encoding='utf-8')
    return tmp_path


def test_run_writes_results_and_trace(bench_dir, capsys):
    assert bench.cmd_run([]) == 0
    res = json.loads((bench_dir / 'harness' / 'runs' / '1' / 'results.json').read_text(encoding='utf-8'))
    assert res['run'] == 1
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
    with pytest.raises(bench.BenchError):
        bench.cmd_run([])


def test_class_of_maps_known_and_unknown_paths():
    assert bench.class_of('flow_engine/gate.py') == 'gates'
    assert bench.class_of('flow_engine/./agents.py') == 'backends'
    assert bench.class_of('flow_engine/unknown.py') is None
