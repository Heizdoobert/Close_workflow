"""agents.slot(): the tests gate and plan.accept take the same FLOW_MAX_AGENTS slot as agent calls."""

import threading

from flow_engine import agents


def _run_slot_once(results, key):
    with agents.slot():
        results[key] = True


def test_slot_enters_and_exits_twice_in_sequence(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_MAX_AGENTS", "1")
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path))
    results = {}

    def run():
        with agents.slot():
            pass
        with agents.slot():
            results["second"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive()
    assert results == {"second": True}


def test_slot_blocks_second_holder_until_first_exits(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOW_MAX_AGENTS", "1")
    monkeypatch.setenv("FLOW_SLOT_DIR", str(tmp_path))
    results = {}

    with agents.slot():
        t = threading.Thread(target=_run_slot_once, args=(results, "other"), daemon=True)
        t.start()
        t.join(1)
        assert t.is_alive()  # the only slot is held here, so the other thread waits
        assert "other" not in results

    t.join(10)
    assert not t.is_alive()
    assert results == {"other": True}
