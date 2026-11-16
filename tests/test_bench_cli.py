import pytest

from flow_engine import bench, cli


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('FLOW_EXEC', raising=False)
    calls = []
    monkeypatch.setattr(cli.engine, 'submit', lambda message, workdir=None: calls.append(message) or 'T1')
    return calls


def test_bench_word_alone_still_submits_task(cli_env, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(bench, 'main', lambda rest: seen.append(rest) or 0)
    assert cli.main(['bench']) == 0
    assert seen == [[]]
    assert cli_env == []


def test_bench_unknown_word_still_submits_task(cli_env, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(bench, 'main', lambda rest: seen.append(rest) or 0)
    assert cli.main(['bench', 'nope']) == 0
    assert seen == [['nope']]
    assert cli_env == []


def test_bench_alone_prints_usage(cli_env, capsys):
    assert cli.main(['bench']) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith('NEXT: flow-task bench run')
    assert cli_env == []


def test_bench_subcommand_is_dispatched(cli_env, monkeypatch):
    seen = []
    monkeypatch.setattr(bench, 'main', lambda rest: seen.append(rest) or 0)
    assert cli.main(['bench', 'run']) == 0
    assert seen == [['run']]
    assert cli_env == []


def test_bench_main_unknown_subcommand_prints_usage_and_next(capsys):
    assert bench.main(['nope']) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1].startswith('NEXT: flow-task bench run')


def test_bench_main_change_without_flags_prints_next(capsys):
    assert bench.main(['change']) == 1
    lines = capsys.readouterr()
    assert lines.err.startswith('bench error: ')
    assert lines.out.splitlines()[-1].startswith('NEXT: flow-task bench change')


def test_help_lists_bench_commands(capsys):
    assert cli.main(['--help']) == 0
    assert 'flow-task bench run' in capsys.readouterr().out
