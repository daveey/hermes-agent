"""Routing-state lock contention: long readers, slow prepare, concurrent allocators. [daveey]

Hub incident (2026-11-02 21:23-21:30 UTC): a maintenance dry run held one read transaction on
``model-routing.sqlite3`` for 8.5 minutes. In the rollback journal every allocator's COMMIT needs
that reader gone, so each timed out at 30 s ("database is locked" at COMMIT), and the PENDING lock
it held while waiting refused every new reader: worker startups failed at the schema check and
``peek``. Per-turn sticky re-resolution also took the writer lock across ``prepare`` (credential
selection), queueing every live agent behind any slow first allocation.

Every case uses a real file and separate SQLite connections (threads or processes); nothing here
mocks SQLite. The busy deadline is shortened so a regression fails in about a second instead of 30.
"""
import json
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing

import pytest

import agent.model_routing_state as mrs
from agent.model_routing import ModelRoutingError, resolve_model_route
from agent.model_routing_state import RoutingState
from tests.agent.test_model_routing import policy

FAST = 1.0  # seconds; regressions fail on this deadline (old code ignored it and waited 30 s)


@pytest.fixture(autouse=True)
def short_busy(monkeypatch):
    monkeypatch.setattr(mrs, "BUSY_TIMEOUT_SECONDS", FAST, raising=False)


def route(path, session, **kw):
    return resolve_model_route("class:deep", config=policy(), session_id=session, state_path=path, **kw)


def rows(path, sql):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(sql).fetchall()


class LongReader:
    """A read transaction held open, as the hydrate dry run did (``BEGIN`` + streamed SELECTs)."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.execute("BEGIN")
        self.db.execute("SELECT * FROM routes").fetchall()

    def close(self):
        self.db.execute("COMMIT")
        self.db.close()


def timed(fn, *args, **kw):
    started = time.monotonic()
    result = fn(*args, **kw)
    return result, time.monotonic() - started


def test_long_reader_does_not_block_a_new_allocation_commit(tmp_path):
    path = tmp_path / "routes.db"
    first = route(path, "existing")
    reader = LongReader(path)
    try:
        second, elapsed = timed(route, path, "new-startup")
        assert elapsed < FAST
    finally:
        reader.close()
    assert second["provider"] != first["provider"]
    assert route(path, "new-startup") == second
    assert rows(path, "PRAGMA journal_mode") == [("wal",)]


def test_peek_schema_and_sticky_reads_survive_a_reader_plus_waiting_writer(tmp_path):
    """The line-20/peek failure: a writer stuck at COMMIT behind a long reader refused new readers."""
    path = tmp_path / "routes.db"
    first = route(path, "existing")
    reader = LongReader(path)
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO aliases VALUES ('w', 'w')")
        try:
            writer.execute("COMMIT")  # rollback journal: BUSY, and the PENDING lock is kept
        except sqlite3.OperationalError:
            pass
        peeked, elapsed = timed(RoutingState(path).peek, "existing", "deep")
        assert peeked == first["metadata"] and elapsed < FAST
        again, elapsed = timed(route, path, "existing")
        assert again == first and elapsed < FAST
        fresh, elapsed = timed(route, path, "another-startup")
        assert fresh["metadata"]["reason"] == "allocation" and elapsed < FAST
    finally:
        if writer.in_transaction:
            writer.execute("ROLLBACK")
        writer.close()
        reader.close()


def test_slow_prepare_holds_only_first_allocations_not_sticky_reuse_or_peek(tmp_path):
    path = tmp_path / "routes.db"
    sticky = route(path, "running-agent")
    entered, release = threading.Event(), threading.Event()
    outcome = {}

    def slow_prepare(metadata):
        entered.set()
        assert release.wait(10)

    def first_allocation():
        try:
            outcome["result"] = route(path, "slow-start", prepare=slow_prepare)
        except BaseException as exc:  # surfaced by the assertions below
            outcome["error"] = exc

    thread = threading.Thread(target=first_allocation)
    thread.start()
    try:
        assert entered.wait(10)
        prepared = []
        again, elapsed = timed(route, path, "running-agent", prepare=prepared.append)
        assert again == sticky and elapsed < FAST
        assert prepared == [sticky["metadata"]]  # sticky reuse is still prepared, exactly once
        state = RoutingState(path)
        assert state.peek("running-agent", "deep") == sticky["metadata"]
        assert state.peek("slow-start", "deep") is None  # unprepared allocation stays invisible
        # A second first allocation queues behind the writer lock and gives up at the deadline.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            route(path, "queued-start")
    finally:
        release.set()
        thread.join(10)
    assert "error" not in outcome
    assert RoutingState(path).peek("slow-start", "deep") == outcome["result"]["metadata"]
    assert RoutingState(path).peek("queued-start", "deep") is None


def test_busy_deadline_exhaustion_publishes_nothing_and_never_prepares(tmp_path):
    path = tmp_path / "routes.db"
    sticky = route(path, "existing")
    counters = rows(path, "SELECT * FROM counters")
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    prepared = []
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            route(path, "blocked", prepare=prepared.append)
        assert FAST * 0.9 <= time.monotonic() - started < FAST * 3  # bounded: one busy deadline
        assert prepared == []
        # Reads are not writers: sticky reuse and peek still answer under a held writer lock.
        again, elapsed = timed(route, path, "existing")
        assert again == sticky and elapsed < FAST
        assert RoutingState(path).peek("existing", "deep") == sticky["metadata"]
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert rows(path, "SELECT * FROM counters") == counters
    assert rows(path, "SELECT count(*) FROM aliases WHERE session='blocked'") == [(0,)]
    assert route(path, "blocked")["metadata"]["reason"] == "allocation"


def _allocate_twice(args):
    path, index = args
    first = route(path, f"s{index}")
    assert route(path, f"s{index}") == first  # sticky re-resolution from another process
    return first["provider"]


def test_concurrent_process_allocators_under_a_long_reader_stay_unique_and_balanced(tmp_path):
    path = tmp_path / "routes.db"
    route(path, "seed")  # create the file (WAL) before the reader opens
    reader = LongReader(path)
    try:
        with ProcessPoolExecutor(max_workers=4) as pool:
            counts = Counter(pool.map(_allocate_twice, [(str(path), i) for i in range(59)]))
    finally:
        reader.close()
    counts[route(path, "seed")["provider"]] += 1
    assert counts == {"anthropic": 20, "openai-codex": 20, "xai-oauth": 20}
    assert rows(path, "SELECT position FROM counters") == [(60,)]
    assert rows(path, "SELECT count(*), count(DISTINCT session) FROM aliases") == [(60, 60)]


def test_concurrent_same_session_allocators_share_one_decision(tmp_path):
    path = tmp_path / "routes.db"
    barrier = threading.Barrier(6)
    results, errors = [], []

    def worker():
        try:
            barrier.wait(10)
            results.append(route(path, "shared", parent_session_id="parent"))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert len({json.dumps(r, sort_keys=True) for r in results}) == 1
    assert rows(path, "SELECT position FROM counters") == [(1,)]
    assert route(path, "parent") == results[0]


def test_sticky_snapshot_never_advances_counters_and_writes_still_go_through_the_lock(tmp_path):
    path = tmp_path / "routes.db"
    first = route(path, "s")
    counters = rows(path, "SELECT * FROM counters")
    for _ in range(5):
        assert route(path, "s") == first
    assert rows(path, "SELECT * FROM counters") == counters
    # A failed prepare on reuse changes nothing.
    with pytest.raises(RuntimeError):
        route(path, "s", prepare=lambda metadata: (_ for _ in ()).throw(RuntimeError("client failed")))
    assert route(path, "s") == first and rows(path, "SELECT * FROM counters") == counters
    # Exact-pin upgrade is a write: recorded durably.
    pinned = route(path, "s", exact_pin=True)
    assert pinned["metadata"]["exact_pin"] is True
    assert RoutingState(path).peek("s", "deep")["exact_pin"] is True
    with pytest.raises(ModelRoutingError, match="cannot fail over"):
        route(path, "s", excluded=[(first["provider"], first["model"])])
    # Failover of an unpinned sticky route advances the counter once, under the writer lock.
    other = route(path, "t")
    failed = route(path, "t", excluded=[(other["provider"], other["model"])])
    assert failed["provider"] != other["provider"]
    assert failed["metadata"]["reason"] == "same-class-failover"
    # (a failover's candidate set differs, so it counts in its own pool: compare the total)
    assert rows(path, "SELECT sum(position) FROM counters") == [(counters[0][1] + 2,)]
    # replace=True always re-selects, even when the sticky route is reusable.
    replaced = route(path, "t", replace=True)
    assert replaced["metadata"]["reason"] == "explicit-switch"


def test_legacy_rollback_journal_file_converts_and_keeps_rows(tmp_path):
    path = tmp_path / "routes.db"
    first = route(path, "legacy")
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")  # the hub's pre-fix file
    assert rows(path, "PRAGMA journal_mode") == [("delete",)]
    assert route(path, "legacy") == first
    assert rows(path, "PRAGMA journal_mode") == [("wal",)]
    assert route(path, "legacy") == first


def test_conversion_blocked_by_a_reader_does_not_fail_allocation(tmp_path, monkeypatch):
    monkeypatch.setattr(mrs, "WAL_CONVERT_TIMEOUT_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(mrs, "BUSY_TIMEOUT_SECONDS", 10.0, raising=False)
    path = tmp_path / "routes.db"
    route(path, "legacy")
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("PRAGMA journal_mode=DELETE")
    reader = LongReader(path)
    with closing(RoutingState(path)._connect()) as db:  # conversion refused under the reader: fallback
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    timer = threading.Timer(0.6, reader.close)  # outlives the conversion attempt, not the busy deadline
    timer.start()
    try:
        result = route(path, "during-conversion")
    finally:
        timer.join()
    assert result["metadata"]["reason"] == "allocation"
    route(path, "after")
    assert rows(path, "PRAGMA journal_mode") == [("wal",)]


def test_read_only_peek_on_wal_with_and_without_sidecars(tmp_path):
    path = tmp_path / "routes.db"
    assert RoutingState(path).peek("x", "deep") is None and not path.exists()
    first = route(path, "x")
    assert not (tmp_path / "routes.db-wal").exists()  # last writer close checkpoints and removes it
    assert RoutingState(path).peek("x", "deep") == first["metadata"]
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE routes SET metadata=? WHERE root='x'", (json.dumps({"uncommitted": True}),))
    try:
        assert RoutingState(path).peek("x", "deep") == first["metadata"]
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_rejected_first_allocation_leaves_schema_for_peek_and_link(tmp_path):
    """Schema creation commits on its own: a rolled-back first allocation must not roll the tables back."""
    path = tmp_path / "routes.db"
    cfg = policy()
    cfg["model_routing"]["routes"]["deep"] = []
    with pytest.raises(ModelRoutingError):
        resolve_model_route("class:deep", config=cfg, session_id="bad", state_path=path)
    state = RoutingState(path)
    assert state.peek("bad", "deep") is None
    with pytest.raises(ModelRoutingError, match="no allocated parent"):
        state.link_continuation("child", "bad")
    assert rows(path, "SELECT count(*) FROM aliases") == [(0,)]
