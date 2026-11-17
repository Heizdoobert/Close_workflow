import os

import pytest

from flow_engine import agents, engine

KEYS = list(engine.CONFIG_KEYS.values())


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    saved = {k: os.environ.pop(k) for k in KEYS if k in os.environ}
    yield tmp_path
    for k in KEYS:
        os.environ.pop(k, None)
    os.environ.update(saved)


def write(root, text):
    (root / "config.toml").write_text(text, encoding="utf-8")


def test_no_file_changes_nothing(root):
    engine.load_config()
    assert not any(k in os.environ for k in KEYS)
    assert agents.chain_from_env(None, None, "code") == ["claude:haiku"]


def test_models_reach_the_chains(root):
    write(root, '[models]\ntask = "sonnet"\ndefault = "claude:sonnet, claude:haiku"\nbench = "claude:opus"\nbackends = ""\n')
    engine.load_config()
    assert os.environ["FLOW_MODEL"] == "sonnet"
    assert os.environ["FLOW_BENCH_BACKENDS"] == "claude:opus"
    assert "FLOW_BACKENDS" not in os.environ  # empty = unset
    assert agents.chain_from_env(None, None, "general") == ["claude:sonnet", "claude:haiku"]
    assert agents.chain_from_env("claude:haiku", None) == ["claude:haiku"]  # an explicit default (single tasks) is kept


def test_parent_policy_still_wins_over_default(root):
    write(root, '[models]\ndefault = "claude:sonnet"\n')
    engine.load_config()
    assert agents.chain_from_env(None, "gpt-oss-120b-medium") == ["agy:gpt-oss-120b-medium!"]


def test_env_wins_over_file(root, monkeypatch):
    monkeypatch.setenv("FLOW_MODEL", "haiku")
    write(root, '[models]\ntask = "sonnet"\n')
    engine.load_config()
    assert os.environ["FLOW_MODEL"] == "haiku"


@pytest.mark.parametrize("text", ['[models]\ntsak = "x"\n', '[models]\ntask = 5\n', 'models = "x"\n', '[models\n'])
def test_bad_file_is_an_error(root, text):
    write(root, text)
    with pytest.raises(RuntimeError, match="config.toml"):
        engine.load_config()
    assert not any(k in os.environ for k in KEYS)
