"""Actual legacy admission/retirement races; no live boards or threads."""
import threading

import pytest

from plugins.kanban.dashboard import board_legacy, board_rows, board_snapshot


def test_stop_drains_admitted_reader_and_rejects_late_nested_open(tmp_path):
    path = str(tmp_path / "kanban.db")
    entered, finish = threading.Event(), threading.Event()
    stopped = threading.Event()
    errors = []

    def read():
        with board_legacy.borrowed(path):
            entered.set()
            assert finish.wait(5)
            # Already-admitted synchronous children must still finish.
            with board_legacy.borrowed(path):
                pass

    thread = threading.Thread(target=read)
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(TimeoutError):
            board_legacy.stop([path], timeout=0)
        with pytest.raises(board_legacy.LegacyStopped):
            board_rows.version(path)

        def stop():
            try:
                board_legacy.stop([path])
                stopped.set()
            except RuntimeError as exc:
                errors.append(exc)

        drain = threading.Thread(target=stop)
        drain.start()
        assert not stopped.is_set()
        finish.set()
        thread.join(5)
        drain.join(5)
        assert stopped.is_set() and not errors
        with pytest.raises(board_legacy.LegacyStopped):
            board_rows.incremental((path,), path, lambda *a: {}, dict)
    finally:
        finish.set()
        thread.join(5)
        board_legacy.resume([path])
    assert board_rows.version(path) is None


def test_snapshot_refresh_reservation_drains_before_retirement(tmp_path):
    path = str(tmp_path / "kanban.db")
    key = (path,)
    entered, finish = threading.Event(), threading.Event()

    def read():
        entered.set()
        assert finish.wait(5)
        return {"value": "fresh"}

    board_snapshot._publish(key, "old", 0, {"value": "old"})
    try:
        assert board_snapshot.serve(key, "new", read, now=50)["value"] == "old"
        assert entered.wait(5)
        with pytest.raises(TimeoutError):
            board_legacy.stop([path], timeout=0)
        with pytest.raises(board_legacy.LegacyStopped):
            board_snapshot.prewarm(read, key, "later")
        finish.set()
        board_snapshot.join_refresh(5)
        board_legacy.stop([path])
        board_snapshot.retire([path])
        assert key not in board_snapshot._cache
    finally:
        finish.set()
        board_snapshot.join_refresh(5)
        board_legacy.resume([path])
        board_snapshot.retire([path])


def test_retirement_is_path_scoped_and_closes_observer(tmp_path):
    import sqlite3

    paths = [str(tmp_path / name) for name in ("a.db", "b.db")]
    for path in paths:
        from contextlib import closing
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE t (id INTEGER)")
            connection.commit()
    try:
        assert all(board_rows.version(path) is not None for path in paths)
        observer = board_rows._observers[paths[0]]
        board_legacy.stop(paths[:1])
        board_rows.retire(paths[:1])
        assert paths[0] not in board_rows._observers
        assert paths[1] in board_rows._observers
        with pytest.raises(sqlite3.ProgrammingError):
            observer.conn.execute("SELECT 1")
        with pytest.raises(board_legacy.LegacyStopped):
            board_rows.content_version(paths[0])
        board_legacy.resume(paths[:1])
        assert board_rows.version(paths[0]) is not None
    finally:
        board_legacy.resume(paths)
        board_rows.clear()


def test_prewarm_is_joined_and_rollback_restarts_once(monkeypatch):
    from plugins.kanban.dashboard import board_prewarm

    entered, finish = threading.Event(), threading.Event()
    starts = []

    def work(_api):
        starts.append(True)
        entered.set()
        assert finish.wait(5)

    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    monkeypatch.setattr(board_prewarm, "_boot_thread", [None])
    monkeypatch.setattr(board_prewarm, "_stop", threading.Event())
    monkeypatch.setattr(board_prewarm, "prewarm_boards", work)
    first = board_prewarm.start_board_prewarm(None)
    assert first is not None
    try:
        assert entered.wait(5)
        assert board_prewarm.start_board_prewarm(None) is first
        with pytest.raises(TimeoutError):
            board_prewarm.stop_board_prewarm(timeout=0)
        assert board_prewarm._stop.is_set()
        finish.set()
        board_prewarm.stop_board_prewarm()
        assert not first.is_alive()
        second = board_prewarm.start_board_prewarm(None)
        assert second is not None
        assert second is not first
        assert board_prewarm.start_board_prewarm(None) is second
        second.join(5)
        assert starts == [True, True]
    finally:
        finish.set()
        board_prewarm.stop_board_prewarm()


def test_legacy_mount_owns_prewarm_and_retries_failed_close(monkeypatch):
    from plugins.kanban.dashboard import board_prewarm, plugin_api

    calls = []
    monkeypatch.setattr(board_prewarm, "start_board_prewarm", lambda api: calls.append(api))

    def stop():
        calls.append("stop")
        if calls.count("stop") == 1:
            raise TimeoutError("still draining")

    monkeypatch.setattr(board_prewarm, "stop_board_prewarm", stop)
    handle = plugin_api.start_legacy()
    assert calls == [plugin_api]
    with pytest.raises(TimeoutError):
        handle.close()
    handle.close()
    handle.close()
    assert calls == [plugin_api, "stop", "stop"]


def test_startup_binding_retires_scoped_real_legacy_state(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_fake import FakeStore
    from plugins.kanban.dashboard import board_fact_views, board_prewarm

    store = FakeStore()
    scope = Scope("install", "profile", "board")
    ctx = SimpleNamespace(facts=SimpleNamespace(v1=store, scope=scope,
                                               handle=SimpleNamespace(demand=lambda *a: None)))
    calls = []
    path = str(tmp_path / "a.db")
    monkeypatch.setattr(board_prewarm, "start_board_prewarm", lambda api: calls.append("start"))
    monkeypatch.setattr(board_prewarm, "stop_board_prewarm", lambda: calls.append("join"))
    monkeypatch.setattr(board_fact_views, "_installed", None)
    bound = board_fact_views.bind_api(ctx, None, provider=lambda _: None,
                                      stop_legacy=lambda: calls.append("drain"),
                                      legacy_paths=[path], hot_selectors=[])
    board_snapshot._publish((path,), "sig", 0, {"old": True})
    try:
        bound.on_mode("facts", 1)
        assert calls == ["start", "drain", "join"]
        assert (path,) not in board_snapshot._cache
        with pytest.raises(board_legacy.LegacyStopped):
            board_rows.version(path)
        bound.on_mode("legacy", 2)
        bound.on_mode("legacy", 3)
        assert calls == ["start", "drain", "join", "start"]
        assert board_rows.version(path) is None
    finally:
        bound.close()
        board_legacy.resume([path])
