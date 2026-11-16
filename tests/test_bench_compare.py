import json
import os
import shutil

import pytest

from flow_engine import bench


@pytest.fixture
def compare_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'flow_engine').mkdir()
    (tmp_path / 'flow_engine' / 'agents.py').write_text('CHANGED', encoding='utf-8')
    return tmp_path


def write_run(root, n, rewards, complete=None):
    d = root / 'harness' / 'runs' / str(n)
    d.mkdir(parents=True)
    tasks = {name: {'reward': r} for name, r in rewards.items()}
    payload = {'run': n, 'tasks': tasks}
    if complete is not None:
        payload['complete'] = complete
    (d / 'results.json').write_text(json.dumps(payload), encoding='utf-8')


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


def test_incomplete_run_is_skipped_by_latest_run(compare_dir):
    write_run(compare_dir, 1, {'t1': 1}, complete=True)
    write_run(compare_dir, 2, {'t1': 0}, complete=False)
    assert bench.latest_run() == 1


def test_incomplete_previous_run_is_not_compared(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1}, complete=True)
    write_run(compare_dir, 2, {'t1': 0}, complete=False)
    write_run(compare_dir, 3, {'t1': 1}, complete=True)
    assert bench.cmd_compare([]) == 0
    assert 'no previous run' in capsys.readouterr().out
    assert not (compare_dir / 'harness' / 'runs' / '3' / 'eval.json').exists()


def test_results_without_tasks_key_is_bench_error(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1})
    (compare_dir / 'harness' / 'runs' / '2').mkdir(parents=True)
    (compare_dir / 'harness' / 'runs' / '2' / 'results.json').write_text(json.dumps({'run': 2}), encoding='utf-8')
    with pytest.raises(bench.BenchError, match='no tasks object'):
        bench.cmd_compare([])
    assert bench.main(['compare']) == 1
    assert 'bench error' in capsys.readouterr().err


@pytest.mark.parametrize('tasks', [{'t1': 5}, {'t1': {'reward': 'x'}}])
def test_bad_task_entry_is_bench_error(compare_dir, capsys, tasks):
    write_run(compare_dir, 1, {'t1': 1})
    (compare_dir / 'harness' / 'runs' / '2').mkdir(parents=True)
    (compare_dir / 'harness' / 'runs' / '2' / 'results.json').write_text(json.dumps({'run': 2, 'tasks': tasks}), encoding='utf-8')
    assert bench.main(['compare']) == 1
    assert 'bench error' in capsys.readouterr().err


def test_manifest_symlink_target_is_refused(compare_dir):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 0})
    write_manifest(compare_dir, 2, ['t1'])
    (compare_dir / 'target.txt').write_text('SECRET', encoding='utf-8')
    (compare_dir / 'flow_engine' / 'agents.py').unlink()
    os.symlink(compare_dir / 'target.txt', compare_dir / 'flow_engine' / 'agents.py')
    with pytest.raises(bench.BenchError, match='symlink'):
        bench.cmd_compare([])
    assert (compare_dir / 'target.txt').read_text(encoding='utf-8') == 'SECRET'


def write_regressing_manifest(root, snapshot):
    write_run(root, 1, {'t1': 1, 't2': 1})
    write_run(root, 2, {'t1': 0, 't2': 0})
    d = root / 'harness' / 'manifest'
    d.mkdir(parents=True)
    manifest = {'run': 2, 'files': [], 'predict': ['t1'], 'snapshot': snapshot}
    (d / '2.json').write_text(json.dumps(manifest), encoding='utf-8')


def test_restore_checks_all_targets_before_writing(compare_dir, tmp_path):
    outside = tmp_path.parent / f'{tmp_path.name}-outside'
    outside.mkdir()
    (outside / 'secret.py').write_text('OUTSIDE', encoding='utf-8')
    (compare_dir / 'flow_engine' / 'gate.py').symlink_to(outside / 'secret.py')
    (compare_dir / 'flow_engine' / 'method.py').write_text('CHANGED', encoding='utf-8')
    write_regressing_manifest(compare_dir, {'flow_engine/method.py': 'ORIGINAL', 'flow_engine/gate.py': 'ORIGINAL'})
    with pytest.raises(bench.BenchError):
        bench.cmd_compare([])
    assert (compare_dir / 'flow_engine' / 'method.py').read_text(encoding='utf-8') == 'CHANGED'
    assert (outside / 'secret.py').read_text(encoding='utf-8') == 'OUTSIDE'


def test_restore_refuses_paths_outside_cwd(compare_dir, tmp_path):
    # one manifest per case, so each key is checked on its own
    for key in ['../evil.py', str(tmp_path / 'evil.py')]:
        shutil.rmtree(compare_dir / 'harness', ignore_errors=True)
        write_regressing_manifest(compare_dir, {key: 'X'})
        with pytest.raises(bench.BenchError):
            bench.cmd_compare([])
        assert not (compare_dir / 'evil.py').exists()
        assert not (tmp_path.parent / 'evil.py').exists()


def test_restore_refuses_symlinked_parent_dir(compare_dir):
    real = compare_dir / 'flow_engine_real'
    real.mkdir()
    (real / 'agents.py').write_text('OUTSIDE', encoding='utf-8')
    (compare_dir / 'flow_engine' / 'agents.py').unlink()
    (compare_dir / 'flow_engine').rmdir()
    (compare_dir / 'flow_engine').symlink_to(real, target_is_directory=True)
    write_regressing_manifest(compare_dir, {'flow_engine/agents.py': 'ORIGINAL'})
    with pytest.raises(bench.BenchError):
        bench.cmd_compare([])
    assert (real / 'agents.py').read_text(encoding='utf-8') == 'OUTSIDE'


@pytest.mark.parametrize('manifest', [
    {'run': 2, 'files': [], 'predict': ['t1']},
    {'run': 2, 'files': [], 'predict': ['t1'], 'snapshot': ['flow_engine/agents.py']},
    {'run': 2, 'files': [], 'predict': ['t1', 5], 'snapshot': {'flow_engine/agents.py': 'ORIGINAL'}},
])
def test_bad_manifest_is_bench_error(compare_dir, capsys, manifest):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 0})
    d = compare_dir / 'harness' / 'manifest'
    d.mkdir(parents=True)
    (d / '2.json').write_text(json.dumps(manifest), encoding='utf-8')
    assert bench.main(['compare']) == 1
    assert 'bench error' in capsys.readouterr().err


def test_infinite_reward_is_bench_error(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': float('inf')})
    assert bench.main(['compare']) == 1
    err = capsys.readouterr().err
    assert 'bench error' in err
    assert 'run 1' in err
