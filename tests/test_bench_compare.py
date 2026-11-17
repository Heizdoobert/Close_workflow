import json
import os

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
    d.mkdir(parents=True, exist_ok=True)
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
    d.mkdir(parents=True, exist_ok=True)
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


def _assert_refuses_outside_cwd(compare_dir, tmp_path, key):
    write_regressing_manifest(compare_dir, {key: 'X'})
    with pytest.raises(bench.BenchError):
        bench.cmd_compare([])
    assert not (compare_dir / 'evil.py').exists()
    assert not (tmp_path.parent / 'evil.py').exists()


def test_restore_refuses_paths_outside_cwd(compare_dir, tmp_path):
    # relative and absolute key, each checked on its own (the def line stays as it was)
    _assert_refuses_outside_cwd(compare_dir, tmp_path, '../evil.py')
    _assert_refuses_outside_cwd(compare_dir, tmp_path, str(tmp_path / 'evil.py'))


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
    json.dumps({'run': 2, 'files': [], 'predict': ['t1']}),
    json.dumps({'run': 2, 'files': [], 'predict': ['t1'], 'snapshot': ['flow_engine/agents.py']}),
    json.dumps({'run': 2, 'files': [], 'predict': ['t1', 5], 'snapshot': {'flow_engine/agents.py': 'ORIGINAL'}}),
    '[]',
    '5',
    json.dumps({'run': 2, 'files': [], 'predict': 't1', 'snapshot': {'flow_engine/agents.py': 'ORIGINAL'}}),
])
def test_bad_manifest_is_bench_error(compare_dir, capsys, manifest):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 0})
    d = compare_dir / 'harness' / 'manifest'
    d.mkdir(parents=True)
    (d / '2.json').write_text(manifest, encoding='utf-8')
    assert bench.main(['compare']) == 1
    err = capsys.readouterr().err
    assert 'bench error' in err
    assert 'bad manifest for run 2' in err


def test_unparsable_manifest_is_bench_error(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 0})
    d = compare_dir / 'harness' / 'manifest'
    d.mkdir(parents=True)
    (d / '2.json').write_text('nope', encoding='utf-8')
    assert bench.main(['compare']) == 1
    err = capsys.readouterr().err
    assert 'bench error' in err
    assert 'bad manifest for run 2' in err


def test_infinite_reward_is_bench_error(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': float('inf')})
    assert bench.main(['compare']) == 1
    err = capsys.readouterr().err
    assert 'bench error' in err
    assert 'run 1' in err


def write_rich_run(root, n, tasks):
    d = root / 'harness' / 'runs' / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / 'results.json').write_text(json.dumps({'run': n, 'complete': True, 'tasks': tasks}), encoding='utf-8')


def write_risk_manifest(root, n, predict, risk):
    d = root / 'harness' / 'manifest'
    d.mkdir(parents=True, exist_ok=True)
    manifest = {'run': n, 'files': ['flow_engine/agents.py'], 'predict': predict, 'risk': risk,
                'snapshot': {'flow_engine/agents.py': 'ORIGINAL'}}
    (d / f'{n}.json').write_text(json.dumps(manifest), encoding='utf-8')


def test_predict_none_without_regression_keeps_changes(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0, 't2': 1})
    write_run(compare_dir, 2, {'t1': 1, 't2': 1})
    write_manifest(compare_dir, 2, [])
    assert bench.cmd_compare([]) == 0
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'CHANGED'
    ev = read_eval(compare_dir, 2)
    assert ev['restored'] == []
    assert ev['reason'] == ''
    assert 'kept run 2 changes' in capsys.readouterr().out


def test_predict_none_with_regression_beyond_flips_restores(compare_dir):
    write_run(compare_dir, 1, {'t1': 1, 't2': 1})
    write_run(compare_dir, 2, {'t1': 0, 't2': 1})
    write_manifest(compare_dir, 2, [])
    assert bench.cmd_compare([]) == 0
    assert (compare_dir / 'flow_engine' / 'agents.py').read_text(encoding='utf-8') == 'ORIGINAL'
    ev = read_eval(compare_dir, 2)
    assert ev['restored'] == ['flow_engine/agents.py']
    assert ev['reason'] == 'regressions 1 > flips 0'


def test_risk_split_names_expected_and_unexpected(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1, 't2': 1, 't3': 1})
    write_run(compare_dir, 2, {'t1': 0, 't2': 0, 't3': 1})
    write_risk_manifest(compare_dir, 2, ['t3'], ['t2'])
    assert bench.cmd_compare([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'regressions: 1 expected (in --risk), 1 unexpected' in out
    assert '  expected: t2' in out
    assert '  unexpected: t1' in out
    ev = read_eval(compare_dir, 2)
    assert ev['regressions_expected'] == ['t2']
    assert ev['regressions_unexpected'] == ['t1']


def test_no_regression_prints_no_regression_line(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 1})
    write_risk_manifest(compare_dir, 2, ['t1'], [])
    assert bench.cmd_compare([]) == 0
    assert 'regressions:' not in capsys.readouterr().out


def test_regression_line_absent_without_manifest(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1})
    write_run(compare_dir, 2, {'t1': 0})
    assert bench.cmd_compare([]) == 0
    out = capsys.readouterr().out
    assert 'regressions:' not in out
    assert 'no manifest for run 2: nothing to keep or restore' in out
    ev = read_eval(compare_dir, 2)
    assert ev['regressions_expected'] == []
    assert ev['regressions_unexpected'] == ['t1']


def test_kept_message_needs_manifest(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 1})
    write_manifest(compare_dir, 2, ['t1'])
    assert bench.cmd_compare([]) == 0
    assert 'kept run 2 changes' in capsys.readouterr().out


def test_cost_and_turns_totals_printed(compare_dir, capsys):
    write_rich_run(compare_dir, 1, {'t1': {'reward': 0, 'cost_usd': 0.25, 'turns': 3},
                                    't2': {'reward': 1, 'cost_usd': 0.5, 'turns': 4}})
    write_rich_run(compare_dir, 2, {'t1': {'reward': 1, 'cost_usd': 0.5, 'turns': 2},
                                    't2': {'reward': 1, 'cost_usd': 0.25, 'turns': 5}})
    assert bench.cmd_compare([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'cost 0.7500 -> 0.7500 USD (+0.0%)' in out
    assert 'turns 7 -> 7' in out
    ev = read_eval(compare_dir, 2)
    assert ev['cost'] == [0.75, 0.75]
    assert ev['turns'] == [7, 7]


def test_cost_percent_left_out_when_before_is_zero(compare_dir, capsys):
    write_rich_run(compare_dir, 1, {'t1': {'reward': 0, 'cost_usd': 0, 'turns': 1}})
    write_rich_run(compare_dir, 2, {'t1': {'reward': 1, 'cost_usd': 0.1, 'turns': 1}})
    assert bench.cmd_compare([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'cost 0.0000 -> 0.1000 USD' in out
    assert not any('%' in line for line in out if line.startswith('cost'))


def test_cost_percent_shows_increase(compare_dir, capsys):
    write_rich_run(compare_dir, 1, {'t1': {'reward': 1, 'cost_usd': 0.2, 'turns': 1}})
    write_rich_run(compare_dir, 2, {'t1': {'reward': 1, 'cost_usd': 0.3, 'turns': 1}})
    assert bench.cmd_compare([]) == 0
    assert 'cost 0.2000 -> 0.3000 USD (+50.0%)' in capsys.readouterr().out.splitlines()


def test_old_results_without_turns_print_turns_na(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 0})
    write_rich_run(compare_dir, 2, {'t1': {'reward': 1, 'turns': 2}})
    assert bench.cmd_compare([]) == 0
    assert 'turns n/a' in capsys.readouterr().out.splitlines()
    ev = read_eval(compare_dir, 2)
    assert ev['turns'] is None
    assert ev['cost'] == [0, 0]


def test_turns_must_be_int_not_bool(compare_dir, capsys):
    write_rich_run(compare_dir, 1, {'t1': {'reward': 1, 'turns': True}})
    write_rich_run(compare_dir, 2, {'t1': {'reward': 1, 'turns': 2}})
    assert bench.cmd_compare([]) == 0
    assert 'turns n/a' in capsys.readouterr().out.splitlines()


def test_non_numeric_cost_is_bench_error(compare_dir, capsys):
    write_rich_run(compare_dir, 1, {'t1': {'reward': 1, 'cost_usd': 'x'}})
    write_rich_run(compare_dir, 2, {'t1': {'reward': 1, 'cost_usd': 0.1}})
    assert bench.main(['compare']) == 1
    assert 'cost_usd' in capsys.readouterr().err


def test_incomplete_newer_run_warns_and_compares_latest_complete(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1}, complete=True)
    write_run(compare_dir, 2, {'t1': 0}, complete=True)
    write_run(compare_dir, 3, {'t1': 0}, complete=False)
    assert bench.cmd_compare([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == 'warning: run 3 is incomplete, using run 2'
    assert read_eval(compare_dir, 2)['run'] == 2


def test_no_warning_when_latest_run_is_complete(compare_dir, capsys):
    write_run(compare_dir, 1, {'t1': 1})
    write_run(compare_dir, 2, {'t1': 0})
    assert bench.cmd_compare([]) == 0
    assert 'warning' not in capsys.readouterr().out


@pytest.mark.parametrize('risk', ['t1', [5], None, 'x'])
def test_bad_risk_in_manifest_is_bench_error(compare_dir, capsys, risk):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 0})
    d = compare_dir / 'harness' / 'manifest'
    d.mkdir(parents=True)
    manifest = {'run': 2, 'files': [], 'predict': ['t1'], 'risk': risk, 'snapshot': {'flow_engine/agents.py': 'ORIGINAL'}}
    (d / '2.json').write_text(json.dumps(manifest), encoding='utf-8')
    assert bench.main(['compare']) == 1
    assert 'bad manifest for run 2' in capsys.readouterr().err


def test_manifest_without_risk_is_accepted(compare_dir):
    write_run(compare_dir, 1, {'t1': 0})
    write_run(compare_dir, 2, {'t1': 1})
    write_manifest(compare_dir, 2, ['t1'])
    assert bench.cmd_compare([]) == 0
    assert read_eval(compare_dir, 2)['regressions_expected'] == []
