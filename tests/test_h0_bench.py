import subprocess
from types import SimpleNamespace

import pytest

from flow_engine import agents, bench


def test_cmd_run_refuses_a_cwd_with_its_own_engine(tmp_path, monkeypatch):
    (tmp_path / 'flow_engine').mkdir()
    (tmp_path / 'flow_engine' / '__init__.py').write_text('', encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bench.sandbox, 'available', lambda: 'bwrap')
    with pytest.raises(bench.BenchError) as err:
        bench.cmd_run([])
    msg = str(err.value)
    assert str(tmp_path / 'flow_engine') in msg
    assert 'bench run' in msg


def test_cwd_without_flow_engine_is_not_a_mismatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert bench._own_engine_mismatch() is None


def test_run_task_sandboxes_accept_without_claude(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv('FLOW_GATE_SANDBOX', raising=False)
    monkeypatch.setattr(bench.sandbox, 'available', lambda: 'bwrap')
    task = tmp_path / 'harness' / 'bench' / 't1'
    task.mkdir(parents=True)
    (task / 'task.md').write_text('do it', encoding='utf-8')
    (task / 'accept.sh').write_text('exit 0', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)

    def fake_call(chain, prompt, o, trace=None, sleep=None):
        return agents.Result(True, 'x', 0.0, {}), 0.0, {}

    wrapped = []

    def recording_wrap(argv, workdir, claude=True):
        wrapped.append((list(argv), claude))
        return ['wrapped', *argv]

    ran = []

    real_run = subprocess.run

    def fake_run(argv, **kw):
        if argv[0] != 'bash' and argv[0] != 'wrapped':
            return real_run(argv, **kw)
        ran.append(argv)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(bench.agents, 'call', fake_call)
    monkeypatch.setattr(bench.sandbox, 'wrap', recording_wrap)
    monkeypatch.setattr(bench.subprocess, 'run', fake_run)

    r = bench.run_task(task, 1)

    assert r['accept_exit'] == 0
    assert wrapped and wrapped[0][1] is False
    assert wrapped[0][0][0] == 'bash'
    assert ran == [['wrapped', 'bash', str((task / 'accept.sh').resolve())]]


def test_run_task_skips_wrap_when_gate_sandbox_is_off(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('FLOW_GATE_SANDBOX', '0')
    monkeypatch.setattr(bench.sandbox, 'available', lambda: 'bwrap')
    task = tmp_path / 'harness' / 'bench' / 't1'
    task.mkdir(parents=True)
    (task / 'task.md').write_text('do it', encoding='utf-8')
    (task / 'accept.sh').write_text('exit 0', encoding='utf-8')
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    subprocess.run(['git', 'add', '-A'], cwd=tmp_path, check=True)

    def fake_call(chain, prompt, o, trace=None, sleep=None):
        return agents.Result(True, 'x', 0.0, {}), 0.0, {}

    def no_wrap(argv, workdir, claude=True):
        raise AssertionError('sandbox.wrap must not run with FLOW_GATE_SANDBOX=0')

    ran = []

    real_run = subprocess.run

    def fake_run(argv, **kw):
        if argv[0] != 'bash' and argv[0] != 'wrapped':
            return real_run(argv, **kw)
        ran.append(argv)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(bench.agents, 'call', fake_call)
    monkeypatch.setattr(bench.sandbox, 'wrap', no_wrap)
    monkeypatch.setattr(bench.subprocess, 'run', fake_run)

    bench.run_task(task, 1)

    assert ran == [['bash', str((task / 'accept.sh').resolve())]]
