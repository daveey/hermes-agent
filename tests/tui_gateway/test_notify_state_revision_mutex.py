"""Transaction-state properties belong to SessionDB's writer critical section."""
import threading
from types import SimpleNamespace

import pytest

from tui_gateway import notification_idle as idle
from tui_gateway import kanban_replica_observer as observer


def test_busy_writer_never_inspects_sqlite_connection(tmp_path, monkeypatch):
    class BusyWriter:
        @property
        def in_transaction(self):
            pytest.fail("poller entered SQLite while its writer mutex was owned")

    lock = threading.Lock()
    db = SimpleNamespace(db_path=tmp_path / "state.db", _conn=BusyWriter(), _lock=lock)
    monkeypatch.setattr(observer, "fingerprint", lambda path: pytest.fail("busy writer cannot prove a revision"))
    lock.acquire()
    try:
        assert idle.state_revision(db) is None
    finally:
        lock.release()


@pytest.mark.parametrize("active", [False, True])
def test_transaction_property_is_serialized_and_lock_is_released(tmp_path, monkeypatch, active):
    lock = threading.Lock()
    class Writer:
        @property
        def in_transaction(self):
            assert lock.locked(), "SQLite properties need the same lock as writes"
            return active

    db = SimpleNamespace(db_path=tmp_path / "state.db", _conn=Writer(), _lock=lock)
    stamp = object()
    monkeypatch.setattr(observer, "fingerprint", lambda path: stamp)
    assert idle.state_revision(db) is (None if active else stamp)
    assert not lock.locked()


def test_native_authorizer_race_completes_in_subprocess():
    import subprocess
    import sys
    from pathlib import Path

    here = Path(__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(here.with_name("state_revision_mutex_probe.py")), str(here.parents[2])],
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REVISION_FINISHED writer completed; idle revision observed" in result.stdout


def test_property_error_releases_writer_lock(tmp_path):
    lock = threading.Lock()
    class ClosedWriter:
        @property
        def in_transaction(self):
            raise RuntimeError("closed during test")

    db = SimpleNamespace(db_path=tmp_path / "state.db", _conn=ClosedWriter(), _lock=lock)
    with pytest.raises(RuntimeError, match="closed during test"):
        idle.state_revision(db)
    assert not lock.locked()
