"""Reservation admission, cancellation and recursive-owner regression cases."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from hermes_cli import sqlite_safe_read as tracking
from hermes_cli import sqlite_offline_reservations as offline


def test_unrelated_board_open_progress_and_same_file_budget(tmp_path):
    from hermes_cli.kanban_dispatch_owner import connect_with_identity

    path = tmp_path / "reserved.db"

    def open_close(target):
        conn = connect_with_identity(target, timeout=0.1)
        conn.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tracking.offline_file_access(path):
            pool.submit(open_close, tmp_path / "other.db").result(3)
            start = time.monotonic()
            with pytest.raises(sqlite3.OperationalError) as caught:
                pool.submit(open_close, path).result(3)
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
            assert time.monotonic() - start < 2
            assert not tracking.has_live_connection(path)


def test_same_path_raw_owners_serialize(tmp_path, monkeypatch):
    path = tmp_path / "reserved.db"
    waiting = threading.Event()
    original = offline.conflicts

    def observe(*args, **kwargs):
        pending = original(*args, **kwargs)
        if pending:
            waiting.set()
        return pending

    monkeypatch.setattr(offline, "conflicts", observe)

    def raw():
        with tracking.offline_file_access(path):
            return "entered"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tracking.offline_file_access(path):
            pending = pool.submit(raw)
            assert waiting.wait(3)
            assert not pending.done()
        assert pending.result(3) == "entered"


def test_cross_owner_nested_cycle_refuses(tmp_path):
    barrier = threading.Barrier(2)
    release = threading.Barrier(2)

    def raw(first, second):
        with tracking.offline_file_access(first):
            barrier.wait(3)
            try:
                with pytest.raises(tracking.LiveConnectionError):
                    with tracking.offline_file_access(second):
                        pytest.fail("cross-owner nested reservation admitted")
                with pytest.raises(sqlite3.OperationalError):
                    tracking.connect_tracked(second)
            finally:
                release.wait(3)

    left, right = tmp_path / "left", tmp_path / "right"
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(raw, left, right)
        b = pool.submit(raw, right, left)
        a.result(5)
        b.result(5)


def test_unknown_custom_opener_refuses_before_allocation(tmp_path):
    calls = []

    def custom(path, **kwargs):
        calls.append(path)
        return sqlite3.connect(path, **kwargs)

    with tracking.offline_file_access(tmp_path / "reserved.db"):
        with pytest.raises(sqlite3.OperationalError):
            tracking.connect_tracked(tmp_path / "other.db", connect_fn=custom)
        assert calls == []
        conn = tracking.connect_tracked(tmp_path / "other.db", connect_fn=custom,
                                        tracking_path=tmp_path / "other.db")
        conn.close()
        assert len(calls) == 1


def test_reserve_interruption_cleans_published_entry(tmp_path, monkeypatch):
    path = tmp_path / "board.db"
    original = offline.reserve

    def interrupted(*args):
        original(*args)
        raise KeyboardInterrupt("after publish")

    with monkeypatch.context() as patch:
        patch.setattr(offline, "reserve", interrupted)
        with pytest.raises(KeyboardInterrupt):
            with tracking.offline_file_access(path):
                pytest.fail("entered after interrupt")
    conn = tracking.connect_tracked(path, wait_for_tracking=False)
    conn.close()


@pytest.mark.parametrize("target", ["file:relative%20board.db?mode=rwc", "file:relative%20board.db?cache=private"])
def test_relative_uri_resolves_to_reserved_target(tmp_path, monkeypatch, target):
    monkeypatch.chdir(tmp_path)
    with tracking.offline_file_access(tmp_path / "relative board.db"):
        with pytest.raises(sqlite3.OperationalError):
            tracking.connect_tracked(target, uri=True, wait_for_tracking=False)


@pytest.mark.parametrize("target", [":memory:", "file::memory:?cache=shared", "file:named?mode=memory&cache=shared", ""])
def test_memory_and_temporary_databases_do_not_conflict(tmp_path, target):
    with tracking.offline_file_access(tmp_path / "named"):
        conn = tracking.connect_tracked(target, uri=True, wait_for_tracking=False)
        conn.close()


def test_manual_tracking_cannot_publish_during_reservation(tmp_path):
    path = tmp_path / "board.db"
    with tracking.offline_file_access(path):
        with pytest.raises(sqlite3.OperationalError):
            tracking.track_connection(path)
    assert not tracking.has_live_connection(path)


def test_recursive_registry_wait_refuses_without_pinning_owner(tmp_path):
    path = tmp_path / "board.db"
    reserved, release = threading.Event(), threading.Event()

    def owner():
        with tracking.offline_file_access(path):
            reserved.set()
            assert release.wait(3)
            assert not tracking.has_live_connection(path)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(owner)
        assert reserved.wait(3)
        try:
            with tracking._live_lock:
                with pytest.raises(sqlite3.OperationalError):
                    tracking.connect_tracked(path)
        finally:
            release.set()
        pending.result(3)
