"""A managed worker adopts only its own session's orphaned background jobs. [daveey]

Regression for proc_70810e2dae05 (Desktop chat 20261112_002413_39be4c, hermes-hub, 2026-11-13):
the always-on Desktop backend that spawned the ``persist_on_release`` + ``notify_on_complete`` job
rolled over at 00:10:01, and a Kanban managed worker started 17 s later. The worker's startup
``recover_from_checkpoint()`` adopted every orphan on the host, this Desktop job included. Its writer
stamp then fenced the successor Desktop backend's re-adoption (``process_manage`` -> not_found at
00:11:38), and the worker's log reader collected the exit at 00:15:02 into a completion queue nobody
reads. The receipt was written and the chat never got its notice.
"""
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from tests.gateway.test_managed_turn_contract import assignment
from tests.gateway.test_managed_worker_route_key import _execute
from tools.process_registry import ProcessRegistry
from tools.process_registry_readopt import recover_tui_orphaned_processes

DESKTOP_KEY = "20261112_002413_39be4c"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _entry(sid: str, pid: int, key: str) -> dict:
    return {
        "session_id": sid, "command": "sleep 60", "pid": pid, "pid_scope": "host",
        "host_start_time": ProcessRegistry._safe_host_start_time(pid), "started_at": time.time(),
        "task_id": f"session:{key}", "owner_task_id": key, "session_key": key, "notify_on_complete": True,
        "persist_on_release": True, "writer_pid": _dead_pid(), "writer_start_time": None,
    }


@pytest.fixture()
def orphans(tmp_path, monkeypatch):
    """Two orphaned jobs (their writers are gone): one of a Desktop chat, one of the worker's own session."""
    import json
    started = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
               for _ in range(2)]
    path = tmp_path / "processes.json"
    path.write_text(json.dumps([_entry("proc_desktop00001", started[0].pid, DESKTOP_KEY),
                                _entry("proc_ownturn00001", started[1].pid, "managed-contract")]))
    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: True)
    with patch("tools.process_registry.CHECKPOINT_PATH", path):
        yield path
    for proc in started:
        proc.kill()
        proc.wait()


def _worker_recovery_kwargs(tmp_path, monkeypatch) -> dict:
    """Run the real managed-worker ``execute`` and capture exactly how it asks for recovery."""
    calls = []
    frame, _ = assignment(tmp_path, monkeypatch)
    real_setattr = monkeypatch.setattr

    def setattr_spy(target, *args, **kwargs):
        if target == 'tools.process_registry.process_registry.recover_from_checkpoint':
            args = (lambda *a, **k: calls.append((a, k)),)
        return real_setattr(target, *args, **kwargs)
    monkeypatch.setattr = setattr_spy
    try:
        _execute(frame, monkeypatch)
    finally:
        monkeypatch.setattr = real_setattr
    assert len(calls) == 1, calls
    args, kwargs = calls[0]
    assert not args, args
    return kwargs


def test_managed_worker_leaves_a_desktop_chats_orphan_to_the_desktop_backend(tmp_path, monkeypatch, orphans):
    kwargs = _worker_recovery_kwargs(tmp_path, monkeypatch)
    worker = ProcessRegistry()
    worker.recover_from_checkpoint(**kwargs)
    # The worker's own session's job is still adopted (F24: process_manage in this turn reaches it) ...
    assert worker.get("proc_ownturn00001") is not None
    # ... but another chat's orphan is not: this process cannot deliver its completion.
    assert worker.get("proc_desktop00001") is None
    # The entry stays an orphan for its owner, so the Desktop backend's keeper adopts it and wakes the chat.
    # (Both registries live in this one test process, so the keeper may also see the worker's own entry.)
    desktop = ProcessRegistry()
    assert recover_tui_orphaned_processes(desktop) >= 1
    adopted = desktop.get("proc_desktop00001")
    assert adopted is not None and adopted.session_key == DESKTOP_KEY
    assert adopted._completion_via_readopt_sink is True


def test_session_scoped_recovery_leaves_other_entries_untouched(orphans):
    import json
    before = {e["session_id"]: e for e in json.loads(orphans.read_text())}
    registry = ProcessRegistry()
    assert registry.recover_from_checkpoint(session_keys=("managed-contract",)) == 1
    after = {e["session_id"]: e for e in json.loads(orphans.read_text())}
    # The other chat's entry keeps its original (dead) writer stamp: nothing fenced it off.
    assert after["proc_desktop00001"]["writer_pid"] == before["proc_desktop00001"]["writer_pid"]
    assert registry.get("proc_desktop00001") is None
