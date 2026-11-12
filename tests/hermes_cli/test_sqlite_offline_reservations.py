"""Raw-I/O reservations exclude same-file opens without pinning unrelated closes."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from hermes_cli import sqlite_safe_read as tracking


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm"])
@pytest.mark.parametrize("alias", ["plain", "uri", "symlink"])
def test_reserved_file_refuses_open_before_allocation(tmp_path, suffix, alias):
    path = tmp_path / "space board.db"
    sqlite3.connect(path).close()
    target = path
    options = {}
    if alias == "uri":
        target = path.as_uri() + "?mode=ro"
        options["uri"] = True
    elif alias == "symlink":
        target = tmp_path / "alias.db"
        target.symlink_to(path)
    with tracking.offline_file_access(str(path) + suffix):
        with pytest.raises(sqlite3.OperationalError) as caught:
            tracking.connect_tracked(target, wait_for_tracking=False, **options)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        other = tracking.connect_tracked(tmp_path / "other.db", wait_for_tracking=False)
        other.close()
    conn = tracking.connect_tracked(target, **options)
    conn.close()
    assert not tracking.has_live_connection(path)


def test_same_thread_wait_refuses_and_nested_raw_cleanup(tmp_path):
    path = tmp_path / "board.db"
    with pytest.raises(ValueError, match="body"):
        with tracking.offline_file_access(path):
            with tracking.offline_file_access(str(path) + "-wal"):
                with pytest.raises(sqlite3.OperationalError):
                    tracking.connect_tracked(path)
            raise ValueError("body")
    conn = tracking.connect_tracked(path)
    with pytest.raises(tracking.LiveConnectionError):
        with tracking.offline_file_access(path):
            pytest.fail("live handle allowed raw access")
    conn.close()
    with tracking.offline_file_access(path):
        pass


def test_waiting_open_and_header_probe_wait_for_reservation(tmp_path, monkeypatch):
    from hermes_cli import sqlite_offline_reservations as offline

    path = tmp_path / "board.db"
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE example (value)")
    seed.close()
    attempted, probed = threading.Event(), threading.Event()
    original = offline.conflicts

    def observe(*args, **kwargs):
        pending = original(*args, **kwargs)
        if pending:
            (probed if kwargs.get("raw") else attempted).set()
        return pending

    monkeypatch.setattr(offline, "conflicts", observe)

    def open_close():
        conn = tracking.connect_tracked(path)
        conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with tracking.offline_file_access(path):
            pending = pool.submit(open_close)
            assert attempted.wait(5)
            assert not pending.done()
        pending.result(5)  # opened and closed again: the probe below races no live handle
        with tracking.offline_file_access(path):
            probing = pool.submit(tracking.read_header_bytes_preopen, path)
            assert probed.wait(5)
            assert not probing.done()
        assert probing.result(5).startswith(b"SQLite")
    header = tracking.read_header_bytes_preopen(path)
    assert header is not None and header.startswith(b"SQLite")


def test_completion_does_not_need_registry_lock(tmp_path):
    path = tmp_path / "board.db"
    reserved, finish = threading.Event(), threading.Event()

    def raw():
        with tracking.offline_file_access(path):
            reserved.set()
            assert finish.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(raw)
        assert reserved.wait(5)
        with tracking._live_lock:
            finish.set()
            pending.result(5)
            conn = tracking.connect_tracked(path)
            conn.close()
