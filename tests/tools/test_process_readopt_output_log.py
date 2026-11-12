"""A Desktop restart must route file-backed completions to unopened chats."""
import json
import threading

import pytest

from tools.process_registry import ProcessRegistry
from tests.tools import test_process_orphan_readopt_tui as helpers

TUI_KEY = helpers.TUI_KEY
_entry = helpers._entry
_kill = helpers._kill
checkpoint = helpers.checkpoint
procs = helpers.procs


@pytest.mark.parametrize("exit_before_recovery", [False, True])
def test_readopted_log_wakes_owning_chat_once(
    checkpoint, procs, tmp_path, monkeypatch, exit_before_recovery,
):
    child = procs()
    log = tmp_path / "proc_restart.log"
    log.write_text("finished persisted work\n")
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    entry = _entry("proc_restart", child.pid, TUI_KEY)
    entry.update(output_log=str(log), persist_on_release=True)
    checkpoint.write_text(json.dumps([entry]))
    if exit_before_recovery:
        log.with_suffix(".exit").write_text("0\n")
        _kill(child)
    registry = ProcessRegistry()
    delivered = []
    ready = threading.Event()

    def wake(event):
        delivered.append(event)
        ready.set()

    registry.on_unknown_exit = wake
    assert registry.recover_from_checkpoint(owner="tui") == 1
    session = registry.get("proc_restart")
    assert session is not None
    if not exit_before_recovery:
        log.with_suffix(".exit").write_text("0\n")
        _kill(child)
    assert session._completion_event.wait(5)
    assert ready.wait(1), "completion never reached the unopened chat's wake sink"
    assert len(delivered) == 1
    assert delivered[0]["session_key"] == TUI_KEY
    assert delivered[0]["exit_code"] == 0
    assert "finished persisted work" in delivered[0]["output"]
    registry.sweep_detached_once()
    registry.recover_from_checkpoint(owner="tui")
    assert len(delivered) == 1
    assert registry.completion_queue.empty()
