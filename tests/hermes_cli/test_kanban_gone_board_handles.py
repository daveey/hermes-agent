"""A removed board leaves no kept handle pinning its deleted files (``kanban_gone_board_handles``). [daveey]

hermes-hub 2026-11-13: the probe board ``auw-probe-t01a1d6ae`` was removed at 21:06:46, and two gateways still
held descriptors on its deleted ``kanban.db``, ``-wal`` and ``-shm`` 30+ minutes later. Every kept-handle cache
revalidated a path only when that path was read again, and nothing reads a removed board again.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_gone_board_handles as gone
from hermes_cli import kanban_kept_reader as kept
from hermes_cli import kanban_reader_retention as retention
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import sqlite_safe_read
from hermes_cli.kanban_sync_board_watch import BoardWatch, path_gone
from tests.hermes_cli import test_kanban_sync_push_starvation as loop_support
from tui_gateway import notification_idle, notification_reader

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="reads /proc/self/fd")

SQL = "SELECT COUNT(*) FROM tasks"


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    yield hermes
    ksc._WATCH.reset()
    kept.close_all()
    retention.close_all()
    notification_idle.reset_board_signatures()


def _board(slug: str) -> Path:
    kb.create_board(slug)
    return Path(kb.kanban_db_path(board=slug))


def _fds_under(directory: Path) -> list[str]:
    """This process's descriptors on files under *directory* (a deleted file reads ``<path> (deleted)``)."""
    prefix = os.path.realpath(directory) + os.sep
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target.startswith(prefix):
            found.append(target)
    return found


def _remove(path: Path) -> Path:
    directory = path.parent
    shutil.rmtree(directory)
    assert _fds_under(directory), "precondition: the kept handle pins the deleted board"
    return directory


def test_one_sync_loop_pass_closes_the_watch_connection_of_a_removed_board(home, monkeypatch, tmp_path):
    """The acceptance shape: query a board through ``_WATCH`` in one round, remove its directory, and the next
    pass of the real loop (which no longer lists it) leaves no descriptor on its deleted files."""
    path = _board("probe")
    boards = [("probe", path)]
    rounds, _waits = loop_support._fake_loop(monkeypatch, tmp_path, boards)
    removed: list[Path] = []

    def sync_board(slug, board_path, *args):
        rounds[-1].append(slug)
        assert ksc._WATCH.query(board_path, SQL)[1] == (0,)
        removed.append(_remove(board_path))
        boards.clear()  # board_paths() stops listing it, as after a real rmtree
        return ksc.RoundResult(board=slug, mode="primary")

    monkeypatch.setattr(ksc, "sync_board", sync_board)
    ksc.run_forever(lambda: len(rounds) < 3, interval=30)
    assert rounds[0] == ["probe"] and removed
    assert _fds_under(removed[0]) == []


def test_watch_keeps_a_live_board_and_closes_only_the_removed_one(home):
    live, doomed = _board("live"), _board("doomed")
    watch = BoardWatch()
    try:
        first = watch.query(live, SQL)
        assert watch.query(doomed, SQL)[1] == (0,)
        directory = _remove(doomed)
        assert watch.release_gone() == 1
        assert _fds_under(directory) == []
        assert list(watch._conns) == [os.path.realpath(live)]
        assert watch.query(live, SQL)[0] == first[0]  # same kept connection: no reopen
        assert watch.release_gone() == 0
    finally:
        watch.reset()


def test_shared_cached_reader_of_a_removed_board_is_closed(home):
    live, doomed = _board("live"), _board("doomed")
    for path in (live, doomed):
        with activity.cached_ro(path) as conn:
            assert tuple(conn.execute(SQL).fetchone()) == (0,)
    directory = _remove(doomed)
    assert gone.sweep() == 1
    assert _fds_under(directory) == []
    assert list(activity._RO_CACHE) == [str(live)]


def test_notification_signature_sweep_closes_removed_board_only(home):
    live, doomed = _board("live"), _board("doomed")
    for path in (live, doomed):
        notification_idle.board_signature(path, "chat")
    with notification_reader.reading(live) as (live_conn, _):
        pass
    directory = _remove(doomed)
    assert gone.sweep() == 1
    assert _fds_under(directory) == []
    assert list(notification_reader._registry) == [str(live)]
    with notification_reader.reading(live) as (conn, retained):
        assert retained and conn is live_conn
    assert gone.sweep() == 0


def test_borrowed_notification_observer_is_not_closed_or_waited_on(home):
    doomed = _board("doomed")
    results, errors = [], []

    def sweep():
        try:
            results.append(gone.sweep())
        except BaseException as exc:
            errors.append(exc)

    with notification_reader.reading(doomed) as (conn, _):
        directory = _remove(doomed)
        thread = threading.Thread(target=sweep)
        thread.start()
        thread.join(2)
        completed_while_borrowed = not thread.is_alive()
        assert tuple(conn.execute(SQL).fetchone()) == (0,)
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert completed_while_borrowed and results == [0]
    assert gone.sweep() == 1
    assert _fds_under(directory) == []


def test_notification_sweep_defers_lifecycle_busy_close(home):
    from hermes_cli import kanban_generation as generation

    doomed = _board("doomed")
    notification_idle.board_signature(doomed, "chat")
    directory = _remove(doomed)
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with generation.lifecycle_access(doomed, timeout=5):
                ready.set()
                assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        assert gone.sweep() == 0
        assert _fds_under(directory)
        assert str(doomed) in notification_reader._registry
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert gone.sweep() == 1
    assert _fds_under(directory) == []


def test_busy_removed_board_does_not_stop_release_of_the_next(home):
    """One lifecycle-busy removed board must not stop the sweep of the boards behind it."""
    from hermes_cli import kanban_generation as generation

    busy, idle = _board("busy"), _board("idle")
    for path in (busy, idle):
        notification_idle.board_signature(path, "chat")
    busy_dir, idle_dir = _remove(busy), _remove(idle)
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with generation.lifecycle_access(busy, timeout=5):
                ready.set()
                assert release.wait(10)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        # Call the observer's own sweep: gone.sweep() swallows BoardLifecycleBusy itself and would hide a
        # regression in release_gone's per-board handling.
        assert notification_reader.release_gone() == 1
        assert _fds_under(idle_dir) == []
        assert _fds_under(busy_dir)
        assert list(notification_reader._registry) == [str(busy)]
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert gone.sweep() == 1
    assert _fds_under(busy_dir) == []


def test_notification_sweep_rechecks_path_after_acquiring_custody(home, monkeypatch):
    from hermes_cli import kanban_sync_board_watch as watch_module

    live = _board("live")
    with notification_reader.reading(live) as (old, _):
        pass
    calls = []

    def temporarily_gone(path):
        calls.append(path)
        return len(calls) == 1

    monkeypatch.setattr(watch_module, "path_gone", temporarily_gone)
    assert notification_reader.release_gone() == 0
    assert len(calls) == 2
    with notification_reader.reading(live) as (conn, retained):
        assert retained and conn is old


def test_notification_sweep_does_not_count_already_evicted_reader(home, monkeypatch):
    from hermes_cli import kanban_sync_board_watch as watch_module

    doomed = _board("doomed")
    notification_idle.board_signature(doomed, "chat")
    directory = _remove(doomed)

    def evicted_meanwhile(path):
        notification_reader.close_all()
        return True

    monkeypatch.setattr(watch_module, "path_gone", evicted_meanwhile)
    assert notification_reader.release_gone() == 0
    assert _fds_under(directory) == []


def test_notification_sweep_does_not_retire_a_replaced_registry_entry(home, monkeypatch):
    from hermes_cli import kanban_sync_board_watch as watch_module

    live = _board("live")
    with notification_reader.reading(live) as (conn, _):
        pass
    key = str(live)
    old = notification_reader._registry[key]
    replacement = notification_reader._Reader(key)

    def replaced_meanwhile(path):
        notification_reader._registry[key] = replacement
        return True

    monkeypatch.setattr(watch_module, "path_gone", replaced_meanwhile)
    try:
        assert notification_reader.release_gone() == 0
        assert notification_reader._registry[key] is replacement
        assert not old.retired and old.conn is conn
        assert tuple(conn.execute(SQL).fetchone()) == (0,)
    finally:
        old.close()


def test_notification_sweep_removes_empty_slots_without_counting_a_close(home):
    doomed = _board("doomed")
    entry = notification_reader._reader(str(doomed))
    assert entry is not None and entry.conn is None
    shutil.rmtree(doomed.parent)
    assert notification_reader.release_gone() == 0
    assert str(doomed) not in notification_reader._registry


def test_a_borrowed_shared_reader_is_never_closed_under_its_borrower(home):
    doomed = _board("doomed")
    with activity.cached_ro(doomed) as conn:
        _remove(doomed)
        assert retention.release_gone() == 0
        assert tuple(conn.execute(SQL).fetchone()) == (0,)  # still usable: the open inode survives the unlink
    assert retention.release_gone() == 1


def test_kept_reader_of_a_removed_board_is_closed_from_another_thread(home):
    """The sweep runs on the housekeeping or sync thread; the kept connections belong to the reading threads."""
    live, doomed = _board("live"), _board("doomed")
    read, release = threading.Event(), threading.Event()
    reopened: list[bool] = []

    def reader():
        for path in (live, doomed):
            with kept.reading(path) as conn:
                assert tuple(conn.execute(SQL).fetchone()) == (0,)
        read.set()
        release.wait(10)
        before = kept.stats["reopens"]
        with kept.reading(live) as conn:  # the live board's kept connection is still served
            assert tuple(conn.execute(SQL).fetchone()) == (0,)
        reopened.append(kept.stats["reopens"] != before)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert read.wait(10)
        directory = _remove(doomed)
        assert gone.sweep() == 1
        assert _fds_under(directory) == []
    finally:
        release.set()
        thread.join(10)
    assert reopened == [False]


def test_a_borrowed_kept_reader_is_never_closed_under_its_borrower(home):
    doomed = _board("doomed")
    with kept.reading(doomed) as conn:
        _remove(doomed)
        assert kept.release_gone() == 0
        assert tuple(conn.execute(SQL).fetchone()) == (0,)
    assert kept.release_gone() == 1


def test_sweep_releases_lock_safe_pread_fds_of_a_removed_board(home):
    doomed = _board("doomed")
    assert sqlite_safe_read.pread_lock_safe(doomed, 0, 16) == b"SQLite format 3\x00"
    directory = _remove(doomed)
    gone.sweep()
    assert _fds_under(directory) == []


def test_only_a_missing_path_counts_as_gone(tmp_path, monkeypatch):
    present = tmp_path / "kanban.db"
    present.write_bytes(b"")
    assert not path_gone(str(present))
    assert path_gone(str(tmp_path / "removed" / "kanban.db"))
    assert path_gone(str(present / "under-a-file"))  # NotADirectoryError: a file now where a directory was

    def denied(_path, *a, **kw):
        raise PermissionError("EACCES")

    monkeypatch.setattr(os, "stat", denied)
    assert not path_gone(str(present))  # unreadable is not removed: the handle stays until a read revalidates


def test_sweep_skips_caches_whose_module_was_never_imported(monkeypatch):
    for name in ("hermes_cli.kanban_sync_client", "hermes_cli.kanban_session_activity",
                 "hermes_cli.kanban_kept_reader", "hermes_cli.sqlite_safe_read", "tui_gateway.notification_reader"):
        monkeypatch.delitem(__import__("sys").modules, name)
    assert gone.sweep() == 0


def test_a_sqlite_error_closing_one_cache_does_not_stop_the_others(home, monkeypatch):
    doomed = _board("doomed")
    with kept.reading(doomed):
        pass

    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(retention, "release_gone", broken)
    _remove(doomed)
    assert gone.sweep() == 1  # the kept reader still closed


def test_a_kept_reader_closed_meanwhile_is_not_counted_twice(home, monkeypatch):
    """Generation recovery may close the entry between the registry snapshot and the sweep's lock."""
    from hermes_cli import kanban_sync_board_watch as watch_module

    doomed = _board("doomed")
    with kept.reading(doomed):
        pass
    entry = next(e for e in kept._registry if not e.closed)

    def closed_meanwhile(path):
        entry.closed = True  # what kept._close sets first
        return True

    monkeypatch.setattr(watch_module, "path_gone", closed_meanwhile)
    try:
        assert kept.release_gone() == 0
    finally:
        entry.closed = False


def test_a_shared_reader_reopened_meanwhile_is_kept(home, monkeypatch):
    """A borrower may replace the cache entry (a reopen) between the sweep's snapshot and its lock."""
    from hermes_cli import kanban_sync_board_watch as watch_module

    doomed = _board("doomed")
    with activity.cached_ro(doomed):
        pass
    key = str(doomed)
    old = activity._RO_CACHE[key]
    newer = (old[0], old[1], threading.Lock(), old[3])

    def reopened_meanwhile(path):
        activity._RO_CACHE[key] = newer
        return True

    monkeypatch.setattr(watch_module, "path_gone", reopened_meanwhile)
    assert retention.release_gone() == 0
    assert activity._RO_CACHE[key] is newer


def test_gateway_housekeeping_runs_the_sweep(monkeypatch):
    import gateway.run as gateway_run
    from tests.gateway.test_checkpoint_prune_housekeeping import _OneTickStopEvent

    calls = []
    monkeypatch.setattr(gone, "sweep", lambda: calls.append(True) or 0)
    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)
    assert calls == [True]
