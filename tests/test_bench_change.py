import json

import pytest

from flow_engine import bench


@pytest.fixture
def change_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / 'harness' / 'bench' / 't1'
    d.mkdir(parents=True)
    (d / 'task.md').write_text('do it', encoding='utf-8')
    (d / 'accept.sh').write_text('exit 0', encoding='utf-8')
    (tmp_path / 'flow_engine').mkdir()
    (tmp_path / 'flow_engine' / 'agents.py').write_text('ORIGINAL', encoding='utf-8')
    (tmp_path / 'flow_engine' / 'method.py').write_text('METHOD', encoding='utf-8')
    return tmp_path


VALID = ['--file', 'flow_engine/agents.py', '--evidence', 'E', '--root', 'R', '--fix', 'X', '--predict', 't1']


def test_valid_change_writes_manifest_with_snapshot(change_dir, capsys):
    assert bench.cmd_change(VALID) == 0
    data = json.loads((change_dir / 'harness' / 'manifest' / '1.json').read_text(encoding='utf-8'))
    assert data['snapshot']['flow_engine/agents.py'] == 'ORIGINAL'
    assert data['predict'] == ['t1']
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: flow-task bench run'


def test_three_files_rejected(change_dir):
    args = ['--file', 'flow_engine/agents.py', '--file', 'flow_engine/method.py', '--file', 'flow_engine/agents.py'] + VALID[2:]
    with pytest.raises(bench.BenchError, match='1 or 2 files'):
        bench.cmd_change(args)


def test_missing_root_rejected(change_dir):
    args = ['--file', 'flow_engine/agents.py', '--evidence', 'E', '--fix', 'X', '--predict', 't1']
    with pytest.raises(bench.BenchError, match='missing --root'):
        bench.cmd_change(args)


def test_path_outside_components_rejected(change_dir):
    (change_dir / 'README.md').write_text('readme', encoding='utf-8')
    args = ['--file', 'README.md'] + VALID[2:]
    with pytest.raises(bench.BenchError, match='outside'):
        bench.cmd_change(args)


def test_unknown_task_rejected(change_dir):
    args = VALID[:-1] + ['nope']
    with pytest.raises(bench.BenchError, match='unknown task'):
        bench.cmd_change(args)


def test_first_run_number_is_one(change_dir):
    assert not (change_dir / 'harness' / 'runs').exists()
    assert bench.cmd_change(VALID) == 0
    assert (change_dir / 'harness' / 'manifest' / '1.json').is_file()


def test_second_change_before_run_is_refused(change_dir):
    assert bench.cmd_change(VALID) == 0
    (change_dir / 'flow_engine' / 'agents.py').write_text('SECOND', encoding='utf-8')
    with pytest.raises(bench.BenchError):
        bench.cmd_change(VALID)
    data = json.loads((change_dir / 'harness' / 'manifest' / '1.json').read_text(encoding='utf-8'))
    assert data['snapshot']['flow_engine/agents.py'] == 'ORIGINAL'
