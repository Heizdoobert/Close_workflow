import json

import pytest

from flow_engine import bench


@pytest.fixture
def analyze_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_run(root, n, rewards):
    d = root / 'harness' / 'runs' / str(n)
    (d / 'trace').mkdir(parents=True)
    tasks = {name: {'reward': r} for name, r in rewards.items()}
    (d / 'results.json').write_text(json.dumps({'run': n, 'tasks': tasks}), encoding='utf-8')
    return d


def write_trace(d, name, lines):
    (d / 'trace' / f'{name}.jsonl').write_text(''.join(json.dumps(x) + '\n' for x in lines), encoding='utf-8')


def setup_failing(root):
    d = write_run(root, 1, {'t1': 0, 't2': 0, 't3': 1})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 2, 'tool': 'Edit', 'file': 'flow_engine/gate.py'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    write_trace(d, 't2', [{'kind': 'accept', 'exit': 1, 'out': 'x'}])
    return d


def test_overview_groups_by_class(analyze_dir, capsys):
    setup_failing(analyze_dir)
    assert bench.cmd_analyze([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'gates: 1 failing: t1 [step 2]' in out
    assert 'unclassified: 1 failing: t2 [step 0]' in out


def test_next_line_names_top_class_file_and_tasks(analyze_dir, capsys):
    setup_failing(analyze_dir)
    assert bench.cmd_analyze([]) == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == ('NEXT: flow-task bench change --file flow_engine/gate.py --evidence <E> --root <R> '
                    '--fix <X> --predict t1')


def test_no_failing_tasks_prints_run_next(analyze_dir, capsys):
    write_run(analyze_dir, 1, {'t1': 1})
    assert bench.cmd_analyze([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == 'no failing tasks in run 1'
    assert out[-1] == 'NEXT: flow-task bench run'


def test_no_runs_raises(analyze_dir):
    with pytest.raises(bench.BenchError, match='no run yet'):
        bench.cmd_analyze([])
