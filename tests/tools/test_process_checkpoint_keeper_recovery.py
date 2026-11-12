"""A replacement backend retries ownership after an overlapping writer exits."""
import json
import threading

import pytest

from tools import process_registry_readopt as readopt
from tools.process_registry import ProcessRegistry
from tests.tools import test_process_orphan_readopt_tui as helpers

checkpoint = helpers.checkpoint
procs = helpers.procs


@pytest.mark.parametrize("desktop", [True, False])
def test_keeper_recovers_after_previous_writer_exits(checkpoint, procs, tmp_path, monkeypatch, desktop):
    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda: desktop)
    monkeypatch.setattr("tools.process_registry_output_log.output_dir", lambda: tmp_path)
    writer, child = procs(), procs()
    log = tmp_path / "proc_overlap.log"
    log.write_text("overlapping restart completed\n")
    entry = helpers._entry("proc_overlap", child.pid, helpers.TUI_KEY)
    entry.update(writer_pid=writer.pid,
                 writer_start_time=ProcessRegistry._safe_host_start_time(writer.pid),
                 output_log=str(log), persist_on_release=True)
    checkpoint.write_text(json.dumps([entry]))
    registry = ProcessRegistry()
    events = []
    ready = threading.Event()

    def wake(event):
        events.append(event)
        ready.set()

    registry.on_unknown_exit = wake
    assert readopt.recover_tui_orphaned_processes(registry) == 0
    readopt._checkpoint_keeper_tick(registry)
    assert registry.get("proc_overlap") is None
    helpers._kill(writer)
    # The job also exits before the next keeper tick. Its log/exit trap survives.
    log.with_suffix(".exit").write_text("0\n")
    helpers._kill(child)
    readopt._checkpoint_keeper_tick(registry)
    if not desktop:
        assert registry.get("proc_overlap") is None
        assert events == []
        return
    assert ready.wait(5), "a one-shot startup scan stranded the late orphan"
    session = registry.get("proc_overlap")
    assert session is not None
    assert session._completion_event.wait(5)
    readopt._checkpoint_keeper_tick(registry)
    assert len(events) == 1
    assert events[0]["session_key"] == helpers.TUI_KEY
    assert events[0]["exit_code"] == 0
    assert "overlapping restart completed" in events[0]["output"]
