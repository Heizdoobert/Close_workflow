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


@pytest.mark.parametrize('tasks', [{'t1': 5}, {'t1': {'reward': 'x'}}])
def test_bad_task_entry_is_bench_error(analyze_dir, capsys, tasks):
    d = analyze_dir / 'harness' / 'runs' / '1'
    d.mkdir(parents=True)
    (d / 'results.json').write_text(json.dumps({'run': 1, 'tasks': tasks}), encoding='utf-8')
    assert bench.main(['analyze']) == 1
    assert 'bench error' in capsys.readouterr().err


def test_infinite_reward_is_bench_error(analyze_dir, capsys):
    write_run(analyze_dir, 1, {'t1': float('inf')})
    assert bench.main(['analyze']) == 1
    err = capsys.readouterr().err
    assert 'bench error' in err
    assert 'run 1' in err


def test_non_dict_trace_lines_are_skipped(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0})
    lines = ['5', '[1]', json.dumps({'kind': 'tool', 'step': 2, 'tool': 'Edit', 'file': 'flow_engine/gate.py'}), json.dumps({'kind': 'accept', 'exit': 1, 'out': 'x'})]
    (d / 'trace' / 't1.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert bench.main(['analyze']) == 0
    assert 'gates: 1 failing: t1 [turn 2]' in capsys.readouterr().out.splitlines()


def test_overview_groups_by_class(analyze_dir, capsys):
    setup_failing(analyze_dir)
    assert bench.cmd_analyze([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'gates: 1 failing: t1 [turn 2]' in out
    assert 'unclassified: 1 failing: t2 [turn 0]' in out


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
    assert out[0] == 'no failing tasks in run 1: the bench is saturated, add a task that fails under harness/bench/'
    assert out[-1] == 'NEXT: flow-task bench run'


def test_no_runs_raises(analyze_dir):
    with pytest.raises(bench.BenchError, match='no run yet'):
        bench.cmd_analyze([])


def test_analyze_skips_run_without_results(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'accept', 'exit': 1, 'out': 'x'}])
    (analyze_dir / 'harness' / 'runs' / '2' / 'trace').mkdir(parents=True)
    assert bench.cmd_analyze([]) == 0
    assert any(line.startswith('unclassified: 1 failing: t1') for line in capsys.readouterr().out.splitlines())


def test_absolute_event_path_is_classified(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 3, 'tool': 'Edit', 'file': '/tmp/bench-x/repo/flow_engine/gate.py'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench.cmd_analyze([]) == 0
    assert 'gates: 1 failing: t1 [turn 3]' in capsys.readouterr().out.splitlines()


def test_results_without_tasks_key_is_bench_error(analyze_dir, capsys):
    d = analyze_dir / 'harness' / 'runs' / '1'
    (d / 'trace').mkdir(parents=True)
    (d / 'results.json').write_text(json.dumps({'run': 1}), encoding='utf-8')
    with pytest.raises(bench.BenchError, match='no tasks object'):
        bench.cmd_analyze([])
    assert bench.main(['analyze']) == 1
    assert 'bench error' in capsys.readouterr().err


def test_non_str_event_file_is_blanked(tmp_path):
    events = [{'step': 1, 'tool': 'Edit', 'file': 5}, {'step': 1, 'tool': 'Edit', 'file': None}]
    bench._relative_events(events, tmp_path)
    assert [ev['file'] for ev in events] == ['', '']


def test_turn_is_used_for_step_class(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 7, 'turn': 1, 'tool': 'Read', 'file': 'flow_engine/method.py'},
                          {'kind': 'tool', 'step': 7, 'turn': 2, 'tool': 'Edit', 'file': 'flow_engine/gate.py'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench._failing_step_class(d / 'trace' / 't1.jsonl') == (2, 'gates')
    assert bench.cmd_analyze([]) == 0
    assert 'gates: 1 failing: t1 [turn 2]' in capsys.readouterr().out.splitlines()


def test_mixed_steps_and_unclassified(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0, 't2': 0, 't3': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 1, 'tool': 'Edit', 'file': 'flow_engine/method.py'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    write_trace(d, 't2', [{'kind': 'tool', 'step': 4, 'tool': 'Edit', 'file': 'flow_engine/agents.py'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    write_trace(d, 't3', [{'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench.cmd_analyze([]) == 0
    out = capsys.readouterr().out.splitlines()
    assert 'prompts: 1 failing: t1 [turn 1]' in out
    assert 'backends: 1 failing: t2 [turn 4]' in out
    assert 'unclassified: 1 failing: t3 [turn 0]' in out


def test_truncated_trace_line_is_bench_error(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0})
    lines = [json.dumps({'kind': 'tool', 'step': 2, 'tool': 'Edit', 'file': 'flow_engine/gate.py'}),
             '{"kind": "tool"',
             json.dumps({'kind': 'accept', 'exit': 1, 'out': 'x'})]
    (d / 'trace' / 't1.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    assert bench.main(['analyze']) == 1
    err = capsys.readouterr().err
    assert 'bench error' in err
    assert 'bad trace line in t1.jsonl' in err


def test_denied_call_in_last_turn_is_backends(analyze_dir):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 1, 'turn': 1, 'tool': 'Edit', 'file': 'flow_engine/gate.py'},
                          {'kind': 'tool', 'step': 1, 'turn': 2, 'tool': 'Bash', 'file': 'flow_engine/gate.py',
                           'denied': True},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench._failing_step_class(d / 'trace' / 't1.jsonl') == (2, 'backends')


def test_denied_call_in_earlier_turn_keeps_file_class(analyze_dir):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 1, 'turn': 1, 'tool': 'Bash', 'file': '', 'denied': True},
                          {'kind': 'tool', 'step': 1, 'turn': 2, 'tool': 'Edit', 'file': 'flow_engine/gate.py'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench._failing_step_class(d / 'trace' / 't1.jsonl') == (2, 'gates')


def test_denied_must_be_true_not_truthy(analyze_dir):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'step': 1, 'turn': 1, 'tool': 'Edit', 'file': 'flow_engine/gate.py',
                           'denied': 'yes'},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench._failing_step_class(d / 'trace' / 't1.jsonl') == (1, 'gates')
    assert bench._denied(d / 'trace' / 't1.jsonl') == 0


def test_denied_counts_all_denied_tool_events(analyze_dir):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'turn': 1, 'tool': 'Bash', 'denied': True},
                          {'kind': 'tool', 'turn': 2, 'tool': 'Write', 'denied': True},
                          {'kind': 'tool', 'turn': 2, 'tool': 'Read'},
                          {'kind': 'accept', 'exit': 1, 'denied': True}])
    assert bench._denied(d / 'trace' / 't1.jsonl') == 2


def test_denied_missing_trace_is_zero(analyze_dir):
    assert bench._denied(analyze_dir / 'nope.jsonl') == 0


def test_label_shows_denied_count(analyze_dir, capsys):
    d = write_run(analyze_dir, 1, {'t1': 0})
    write_trace(d, 't1', [{'kind': 'tool', 'turn': 3, 'tool': 'Bash', 'denied': True},
                          {'kind': 'tool', 'turn': 3, 'tool': 'Write', 'denied': True},
                          {'kind': 'accept', 'exit': 1, 'out': 'x'}])
    assert bench.cmd_analyze([]) == 0
    assert 'backends: 1 failing: t1 [turn 3, 2 denied]' in capsys.readouterr().out.splitlines()


def test_warns_when_newer_run_is_incomplete(analyze_dir, capsys):
    setup_failing(analyze_dir)
    (analyze_dir / 'harness' / 'runs' / '2' / 'trace').mkdir(parents=True)
    assert bench.cmd_analyze([]) == 0
    assert capsys.readouterr().out.splitlines()[0] == 'warning: run 2 is incomplete, using run 1'
