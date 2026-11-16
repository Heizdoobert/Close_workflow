import json

import pytest

from flow_engine import bench


@pytest.fixture
def compare_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'flow_engine').mkdir()
    (tmp_path / 'flow_engine' / 'agents.py').write_text('CHANGED', encoding='utf-8')
    return tmp_path


def write_run(root, n, rewards):
    d = root / 'harness' / 'runs' / str(n)
    d.mkdir(parents=True)
    tasks = {name: {'reward': r} for name, r in rewards.items()}
    (d / 'results.json').write_text(json.dumps({'run': n, 'tasks': tasks}), encoding='utf-8')


def write_manifest(root, n, predict):
    d = root / 'harness' / 'manifest'
    d.mkdir(parents=True)
    manifest = {'run': n, 'files': ['flow_engine/agents.py'], 'predict': predict,
                'snapshot': {'flow_engine/agents.py': 'ORIGINAL'}}
    (d / f'{n}.json').write_text(json.dumps(manifest), encoding='utf-8')


def read_eval(root, n):
    return json.loads((root / 'harness' / 'runs' / str(n) / 'eval.json').read_text(encoding='utf-8'))


def test_zero_predicted_flips_restores_snapshot(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0, 't2': 1})
    write_run(compare_dir, 2, {'t1': 0, 't2': 1})
    write_manifest(compare_dir, 2, ['t1'])
    assert bench.cmd_compare([]) == 0
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'ORIGINAL'
    ev = read_eval(compare_dir, 2)
    assert ev['restored'] == ['flow_engine/agents.py']
    assert 'predicted' in ev['reason']
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: flow-task bench analyze'


def test_predicted_flip_without_regression_keeps_changes(compare_dir):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 1})
    write_manifest(compare_dir, 2, ['t1'])
    assert bench.cmd_compare([]) == 0
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'CHANGED'
    ev = read_eval(compare_dir, 2)
    assert ev['restored'] == []
    assert ev['predicted_flip'] == 1


def test_regressions_equal_to_flips_keep_changes(compare_dir):
    write_run(compare_dir, 1, {'t1': 1, 't2': 0, 't3': 1})
    write_run(compare_dir, 2, {'t1': 0, 't2': 1, 't3': 1})
    write_manifest(compare_dir, 2, ['t2'])
    assert bench.cmd_compare([]) == 0
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'CHANGED'
    ev = read_eval(compare_dir, 2)
    assert (ev['improvements'], ev['regressions']) == (1, 1)
    assert ev['restored'] == []


def test_more_regressions_than_flips_restores(compare_dir):
    write_run(compare_dir, 1, {'t1': 1, 't2': 0, 't3': 1, 't4': 1})
    write_run(compare_dir, 2, {'t1': 0, 't2': 1, 't3': 1, 't4': 0})
    write_manifest(compare_dir, 2, ['t2'])
    assert bench.cmd_compare([]) == 0
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'ORIGINAL'
    ev = read_eval(compare_dir, 2)
    assert ev['restored'] == ['flow_engine/agents.py']
    assert ev['reason'] == 'regressions 2 > flips 1'


def test_first_run_prints_next_and_writes_no_eval(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1})
    assert bench.cmd_compare([]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: flow-task bench analyze'
    assert not (compare_dir / 'harness' / 'runs' / '1' / 'eval.json').exists()


def test_no_runs_raises(compare_dir):
    with pytest.raises(bench.BenchError, match='no run yet'):
        bench.cmd_compare([])


def test_restore_rejects_unsafe_manifest_path(compare_dir):
    write_run(compare_dir, 1, {'t1': 1, 't2': 1})
    write_run(compare_dir, 2, {'t1': 0, 't2': 0})
    d = compare_dir / 'harness' / 'manifest'
    d.mkdir(parents=True)
    (d / '2.json').write_text(json.dumps({'run': 2, 'files': [], 'predict': ['t1'],
                                          'snapshot': {'../evil.py': 'X', 'flow_engine/agents.py': 'ORIGINAL'}}), encoding='utf-8')
    with pytest.raises(bench.BenchError):
        bench.cmd_compare([])
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'CHANGED'
    assert not (compare_dir / 'evil.py').exists()


def test_latest_run_skips_dir_without_results(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0})
    (compare_dir / 'harness' / 'runs' / '2' / 'trace').mkdir(parents=True)
    assert bench.cmd_compare([]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: flow-task bench analyze'
    assert not (compare_dir / 'harness' / 'runs' / '1' / 'eval.json').exists()


def test_corrupt_results_is_bench_error(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1})
    (compare_dir / 'harness' / 'runs' / '2').mkdir(parents=True)
    (compare_dir / 'harness' / 'runs' / '2' / 'results.json').write_text('nope', encoding='utf-8')
    assert bench.main(['compare']) == 1
    assert 'bench error' in capsys.readouterr().err
