"""Background collection cannot stall ready-lane evaluation or reuse stale publication."""
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_pr_idle_checks as checks
from hermes_cli import kanban_pr_acceptance as acceptance
from tests.hermes_cli import test_kanban_active_pr_idle_green as helpers

kanban_home = helpers.kanban_home
green = helpers.green
pytestmark = pytest.mark.usefixtures("kanban_home")


def test_blocked_network_does_not_block_guard_and_next_tick_releases(green, monkeypatch):
    entered, finish = Event(), Event()
    def collect(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return green[0]
    monkeypatch.setattr(acceptance, "collect_acceptance", collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(checks, "_pool", pool)
        with kbc.connect() as conn:
            tid = helpers.published(conn)
            try:
                assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
                assert entered.wait(5)  # the guard returned while the network is still blocked
                assert not finish.is_set()
                future = next(iter(checks._pending.values()))[1]
            finally:
                finish.set()
            assert future.result(timeout=5)
            assert dispatch.check_respawn_guard(conn, tid) is None


def test_only_two_jobs_and_no_queued_backlog(monkeypatch):
    class HeldPool:
        calls = []
        def submit(self, *args):
            future = Future()
            self.calls.append(future)
            return future
    pool = HeldPool()
    checks.clear_cache()
    monkeypatch.setattr(checks, "_pool", pool)
    try:
        for i in range(40):
            assert not checks.green((i,), helpers.URL, "a", helpers.SHA)
        assert len(pool.calls) == len(checks._pending) == 2
        pool.calls[0].set_result(True)
        assert checks.green((0,), helpers.URL, "a", helpers.SHA)
    finally:
        checks.clear_cache()


def test_late_observation_is_discarded(monkeypatch):
    future = Future()
    clock = [1000.0]
    checks.clear_cache()
    monkeypatch.setattr(checks.time, "monotonic", lambda: clock[0])
    checks._pending[(1,)] = (clock[0], future)
    clock[0] += 121
    future.set_result(True)
    try:
        assert not checks.green((1,), helpers.URL, "a", helpers.SHA)
    finally:
        checks.clear_cache()


def test_new_publication_during_collection_does_not_reuse_green(green, monkeypatch):
    from tests.hermes_cli.kanban_guard_helpers import worker_comment
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        def collect(*args, **kwargs):
            worker_comment(conn, tid, "a", f"Also opened {helpers.URL}0")
            return green[0]
        monkeypatch.setattr(acceptance, "collect_acceptance", collect)
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"


def test_multiple_publications_all_need_green(green, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tests.hermes_cli.kanban_guard_helpers import worker_comment
    other = helpers.URL + "0"
    failing = [other]
    def collect(url, *args, **kwargs):
        return {**green[0], "pr_url": url, "ok": url != failing[0]}
    monkeypatch.setattr(acceptance, "collect_acceptance", collect)
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        conn.execute("UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,))
        conn.commit()
        worker_comment(conn, tid, "a", f"Also opened {other}")
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
        checks.clear_cache()
        failing[0] = None
        assert dispatch.check_respawn_guard(conn, tid) is None


def test_failed_background_observation_stays_held(monkeypatch):
    checks.clear_cache()
    future = Future()
    future.set_exception(ValueError("unreadable"))
    checks._pending[(1,)] = (checks.time.monotonic(), future)
    try:
        assert not checks.green((1,), helpers.URL, "a", helpers.SHA)
    finally:
        checks.clear_cache()


def test_cache_has_a_hard_size_bound(green, monkeypatch):
    monkeypatch.setattr(checks, "MAX_ENTRIES", 3)
    for i in range(8):
        assert checks.green((i,), helpers.URL, "a", helpers.SHA)
    assert len(checks._cache) <= 3
