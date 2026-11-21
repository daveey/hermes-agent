"""A slow kanban annotation refresh never holds a session read past ``_REFRESH_WAIT_S``. [daveey]

On hermes-hub (2026-10-31 23:33 UTC) the one-row ``GET /api/sessions/<id>`` found the refresh lock free, so it ran
the whole annotation read on its own worker. Inside a Desktop poll burst that read took 5.3 s and the Desktop's 5 s
request timeout fired (t_01a19451dffa47a8). A poll with a previous read of its scope now waits a bounded time for
the new read and otherwise serves the previous one, dated by when it was read.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from hermes_cli import kanban_session_activity as ksa

SCOPE = ("annotation-refresh-bounded-test", "default")


def _parts(n):
    return ksa._identity.freeze_annotation(({"s1": n}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}))


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(ksa, "_origin_scope", lambda: SCOPE)
    monkeypatch.setattr(ksa._annotation_copy, "published", lambda scope, parts: None)
    monkeypatch.setattr(ksa._annotation_copy, "seed", lambda *a, **k: None)
    monkeypatch.setattr(ksa, "_REFRESH_WAIT_S", 0.2, raising=False)
    ksa._reset_cache_for_tests()
    yield monkeypatch
    ksa._reset_cache_for_tests()


def _prime(monkeypatch, n=1):
    monkeypatch.setattr(ksa, "_read_annotation_parts", lambda: _parts(n))
    first = ksa._annotation_snapshot()
    assert first[0] == {"s1": n}
    return first


def _slow_read(monkeypatch, n, *, error=None):
    started, release = threading.Event(), threading.Event()

    def read():
        started.set()
        assert release.wait(10)
        if error is not None:
            raise error
        return _parts(n)

    monkeypatch.setattr(ksa, "_read_annotation_parts", read)
    return started, release


def _wait_unlocked(timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ksa._annotate_refresh_lock.acquire(blocking=False):
            ksa._annotate_refresh_lock.release()
            return True
        time.sleep(0.01)
    return False


def test_slow_refresh_serves_previous_read_within_bound(scoped):
    _prime(scoped, 1)
    previous_read_at = time.time()
    started, release = _slow_read(scoped, 2)
    try:
        t0 = time.monotonic()
        rows = ksa.annotate_kanban_activity([{"id": "s1"}])
        elapsed = time.monotonic() - t0
        assert started.is_set()
        assert elapsed < 1.0
        assert rows[0]["kanban_active"] == 1
        assert "kanban_unknown" not in rows[0]
        # The served previous read keeps its own date, never a fresh observation.
        assert rows[0]["kanban_observed_at"] <= previous_read_at
    finally:
        release.set()
    assert _wait_unlocked()
    # The background read published itself: the next poll sees it.
    scoped.setattr(ksa, "_read_annotation_parts", lambda: _parts(3))
    assert ksa._last_annotation[SCOPE][0] == {"s1": 2}
    assert ksa.annotate_kanban_activity([{"id": "s1"}])[0]["kanban_active"] == 3


def test_fast_refresh_still_returns_the_new_read(scoped):
    _prime(scoped, 1)
    scoped.setattr(ksa, "_read_annotation_parts", lambda: _parts(5))
    before = time.time()
    row = ksa.annotate_kanban_activity([{"id": "s1"}])[0]
    assert row["kanban_active"] == 5
    assert row["kanban_observed_at"] >= before - ksa._CACHE_TTL_S - 0.01
    assert _wait_unlocked(0.5)


def test_fast_refresh_error_reaches_the_caller(scoped):
    _prime(scoped, 1)

    def boom():
        raise RuntimeError("board unreadable")

    scoped.setattr(ksa, "_read_annotation_parts", boom)
    with pytest.raises(RuntimeError, match="board unreadable"):
        ksa._annotation_snapshot()
    assert _wait_unlocked(0.5)
    row = ksa.annotate_kanban_activity([{"id": "s1"}])[0]
    assert row.get("kanban_unknown")
    assert "kanban_active" not in row


def test_slow_refresh_error_keeps_previous_and_releases_lock(scoped, caplog):
    _prime(scoped, 1)
    _started, release = _slow_read(scoped, 2, error=RuntimeError("late failure"))
    assert ksa._annotation_snapshot()[0] == {"s1": 1}
    release.set()
    assert _wait_unlocked()
    assert ksa._last_annotation[SCOPE][0] == {"s1": 1}
    assert any("background refresh failed" in r.getMessage() for r in caplog.records)
    scoped.setattr(ksa, "_read_annotation_parts", lambda: _parts(4))
    assert ksa._annotation_snapshot()[0] == {"s1": 4}


@pytest.mark.parametrize("host_age, expected", [(10, 1), (5, 1), (4, 0)])
def test_display_peek_preserves_newer_restart_seed(scoped, host_age, expected):
    from hermes_cli import kanban_annotation_disk as disk

    now = time.time()
    # Exercise the real restart seed path, not a process-local bounded read.
    scoped.undo()
    ksa._reset_cache_for_tests()
    scoped.setattr(ksa, "_origin_scope", lambda: SCOPE)
    scoped.setattr(disk, "load", lambda scope: (_parts(1), now - 5))
    scoped.setattr(ksa._host_copy, "_display_read", lambda scope: (_parts(0), now - host_age, True))
    with ksa._annotate_refresh_lock:
        row = ksa.annotate_kanban_activity([{"id": "s1"}], display=True)[0]
    assert row.get("kanban_active", 0) == expected, row
    assert row["kanban_observed_at"] == now - (5 if expected else host_age)
    assert not row.get("kanban_unknown")


def test_fast_cold_scope_returns_counts_with_caller_context(scoped):
    from contextvars import ContextVar

    scope = ContextVar("cold_scope", default="wrong")
    scope.set("caller")
    reads = []
    scoped.setattr(ksa, "_read_annotation_parts", lambda: reads.append(scope.get()) or _parts(1))
    assert ksa._annotation_snapshot()[0] == {"s1": 1}
    assert reads == ["caller"]


def test_refresh_runs_on_a_real_thread_when_thread_is_stubbed(scoped):
    _prime(scoped, 1)

    class Never:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise AssertionError("stub thread used")

    scoped.setattr(threading, "Thread", Never)
    scoped.setattr(ksa, "_read_annotation_parts", lambda: _parts(6))
    assert ksa._annotation_snapshot()[0] == {"s1": 6}


def test_session_detail_route_answers_within_bound_during_slow_refresh(scoped):
    from hermes_cli.web_routers import cron, sessions

    class DB:
        def resolve_session_id(self, sid):
            return sid

        def get_session(self, sid):
            return {"id": sid}

        def get_compression_chain(self, sid):
            return [sid]

        def close(self):
            pass

    scoped.setattr(sessions, "_open_session_db_for_profile", lambda *a, **kw: DB())
    scoped.setattr(sessions, "_serving_profile", lambda p: p)
    scoped.setattr(cron, "cron_run_scheduler_owned", lambda *a: None)
    _prime(scoped, 1)
    started, release = _slow_read(scoped, 2)
    try:
        t0 = time.monotonic()
        row = asyncio.run(sessions.get_session_detail("s1", "default"))
        assert time.monotonic() - t0 < 1.5
        assert started.is_set()
        assert row["kanban_active"] == 1
        assert "kanban_unknown" not in row
    finally:
        release.set()
    assert _wait_unlocked()
