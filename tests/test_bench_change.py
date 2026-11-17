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
    assert capsys.readouterr().out.splitlines()[-1] == 'NEXT: edit the files, then: flow-task bench run'


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


def test_symlinked_component_file_is_refused(change_dir, capsys):
    outside = change_dir.parent / f'{change_dir.name}-outside.py'
    outside.write_text('OUTSIDE', encoding='utf-8')
    (change_dir / 'flow_engine' / 'agents.py').unlink()
    (change_dir / 'flow_engine' / 'agents.py').symlink_to(outside)
    assert bench.main(['change'] + VALID) == 1
    assert 'bench error' in capsys.readouterr().err
    assert not (change_dir / 'harness' / 'manifest' / '1.json').exists()
    assert outside.read_text(encoding='utf-8') == 'OUTSIDE'


def test_symlinked_component_dir_is_refused(change_dir, capsys):
    outside_dir = change_dir.parent / f'{change_dir.name}-outside-dir'
    outside_dir.mkdir()
    (outside_dir / 'agents.py').write_text('OUTSIDE', encoding='utf-8')
    (change_dir / 'flow_engine' / 'agents.py').unlink()
    (change_dir / 'flow_engine' / 'method.py').unlink()
    (change_dir / 'flow_engine').rmdir()
    (change_dir / 'flow_engine').symlink_to(outside_dir, target_is_directory=True)
    assert bench.main(['change'] + VALID) == 1
    assert 'bench error' in capsys.readouterr().err
    assert not (change_dir / 'harness' / 'manifest' / '1.json').exists()
    assert (outside_dir / 'agents.py').read_text(encoding='utf-8') == 'OUTSIDE'


def test_predict_none_stores_empty_list(change_dir, capsys):
    assert bench.cmd_change(VALID[:-1] + ['none']) == 0
    data = json.loads((change_dir / 'harness' / 'manifest' / '1.json').read_text(encoding='utf-8'))
    assert data['predict'] == []
    assert data['risk'] == []


def test_empty_predict_still_rejected(change_dir):
    with pytest.raises(bench.BenchError, match='missing --predict'):
        bench.cmd_change(VALID[:-1] + [' , '])


def test_risk_is_stored(change_dir):
    assert bench.cmd_change(VALID + ['--risk', 't1']) == 0
    data = json.loads((change_dir / 'harness' / 'manifest' / '1.json').read_text(encoding='utf-8'))
    assert data['risk'] == ['t1']


def test_unknown_risk_task_rejected(change_dir):
    with pytest.raises(bench.BenchError, match='unknown task: nope'):
        bench.cmd_change(VALID + ['--risk', 'nope'])
    assert not (change_dir / 'harness' / 'manifest' / '1.json').exists()


def test_risk_flag_needs_value(change_dir, capsys):
    assert bench.main(['change'] + VALID + ['--risk']) == 1
    assert 'bench error' in capsys.readouterr().err


def test_change_usage_mentions_risk_and_none(capsys):
    assert bench.main(['nope']) == 1
    assert '--risk' in capsys.readouterr().out
    assert '|none' in bench.CHANGE_USAGE
