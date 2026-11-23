"""Source-custody regressions for pickup candidates not imported locally."""
import sqlite3

import pytest

from hermes_cli import kanban_admission_score as score


@pytest.fixture
def sources(monkeypatch):
    from hermes_cli import kanban_db_sync, kanban_worker_node

    def database():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE tasks (id TEXT, priority INTEGER, status TEXT, created_at INTEGER,
                                admission_class TEXT, consecutive_failures INTEGER);
            CREATE TABLE task_events (task_id TEXT, kind TEXT, payload TEXT, created_at INTEGER);
            CREATE TABLE task_runs (task_id TEXT, ended_at INTEGER);
            CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
        """)
        return conn

    local, remote = database(), database()

    class Reader:
        def execute(self, *args):
            return remote.execute(*args)

        def close(self):
            pass

    monkeypatch.setattr(kanban_db_sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(kanban_worker_node, "open_replica", lambda conn: Reader())
    yield local, remote
    local.close()
    remote.close()


def add(conn, tid, start, priority=0, klass="normal", failures=0):
    conn.execute("INSERT INTO tasks VALUES (?, ?, 'ready', ?, ?, ?)",
                 (tid, priority, start, klass, failures))
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


@pytest.mark.parametrize("replica", [False, True])
def test_same_second_ready_ties_use_source_creation_before_random_id(sources, replica):
    local, remote = sources
    source = remote if replica else local
    older = add(source, "z-first", 1000.1)
    newer = add(source, "a-second", 1000.2)
    for tid in ("z-first", "a-second"):
        source.execute("INSERT INTO task_events VALUES (?, 'promoted', '{}', 9000)", (tid,))
    # Minimal candidate projections and reversed input must not erase source FIFO.
    rows = [{"id": newer["id"]}, {"id": older["id"]}]
    ranked = score.rank(local, rows, now_ms=9_001_000, weights={})
    assert [row["id"] for row, _ in ranked] == ["z-first", "a-second"]


def test_ready_episode_precedes_creation_tiebreak(sources):
    local, _ = sources
    older = add(local, "older-card", 1000)
    newer = add(local, "newer-card", 2000)
    local.execute("INSERT INTO task_events VALUES ('older-card', 'promoted', '{}', 9001)")
    local.execute("INSERT INTO task_events VALUES ('newer-card', 'promoted', '{}', 9000)")
    ranked = score.rank(local, [older, newer], now_ms=9_002_000, weights={})
    assert [row["id"] for row, _ in ranked] == ["newer-card", "older-card"]


def test_local_creation_tiebreak_overrides_replica(sources):
    local, remote = sources
    older = add(remote, "z-first", 2000)
    newer = add(remote, "a-second", 1000)
    add(local, "z-first", 1000)
    add(local, "a-second", 2000)
    for source in (local, remote):
        for tid in ("z-first", "a-second"):
            source.execute("INSERT INTO task_events VALUES (?, 'promoted', '{}', 9000)", (tid,))
    ranked = score.rank(local, [newer, older], now_ms=9_001_000, weights={})
    assert [row["id"] for row, _ in ranked] == ["z-first", "a-second"]


def test_replica_candidate_uses_replica_readiness_and_metadata(sources):
    local, remote = sources
    row = add(remote, "remote", 1000, 300, "capacity", 2)
    remote.execute("INSERT INTO task_events VALUES ('remote', 'promoted', '{}', 9000)")
    (_, parts), = score.rank(local, [row], now_ms=9_120_000, weights={})
    assert (parts.ready_since, parts.effective_priority, parts.class_rank, parts.failures) == (9000, 300, 1, 2)
    assert parts.age_buckets == 2


def test_local_readiness_overrides_even_newer_replica_history(sources):
    local, remote = sources
    row = add(remote, "same", 9000, 800, "urgent", 4)
    add(local, "same", 8000, 10, "normal", 1)
    local.execute("INSERT INTO task_runs VALUES ('same', 8500)")
    (_, parts), = score.rank(local, [row], now_ms=9_120_000, weights={})
    assert (parts.ready_since, parts.effective_priority, parts.class_rank, parts.failures) == (8500, 10, 0, 1)
    assert parts.age_buckets == 10


@pytest.mark.parametrize("start", [None, 0])
def test_unknown_readiness_never_ages_from_epoch(sources, start):
    local, _ = sources
    row = add(local, "missing-clock", start)
    (_, parts), = score.rank(local, [row], now_ms=9_120_000, weights={})
    assert parts.age_buckets == 0
    assert parts.ready_since == 9120


def test_vanished_candidate_has_no_epoch_bonus(sources):
    local, remote = sources
    row = add(remote, "gone", 1000)
    remote.execute("DELETE FROM tasks")
    (_, parts), = score.rank(local, [row], now_ms=9_120_000, weights={})
    assert parts.age_buckets == 0


def test_one_hour_reaches_top_band_and_age_does_not_saturate(sources):
    local, _ = sources
    fresh = add(local, "fresh", 10000, 1000)
    old = add(local, "old", 6400)
    older = add(local, "older", 2800)
    ranked = score.rank(local, [fresh, old, older], now_ms=10_000_000, weights={})
    assert [row["id"] for row, _ in ranked] == ["older", "old", "fresh"]
    assert [part.age_buckets for _, part in ranked] == [120, 60, 0]


@pytest.mark.parametrize("failure_at", ["open", "runtime-open", "links", "history"])
def test_replica_failure_discards_partial_snapshot(sources, monkeypatch, failure_at):
    from hermes_cli import kanban_worker_node

    local, remote = sources
    row = add(local, "local", 9000, 400)
    add(remote, "remote", 1000, 1000)
    add(remote, "local", 1000, 800)
    remote.execute("INSERT INTO task_links VALUES ('local', 'remote')")

    class BrokenReader:
        def execute(self, sql, *args):
            if (failure_at == "links" and "FROM task_links" in sql) or (
                    failure_at == "history" and "FROM task_events" in sql):
                raise sqlite3.OperationalError("unavailable")
            return remote.execute(sql, *args)

        def close(self):
            pass

    def open_reader(conn):
        if failure_at == "runtime-open":
            raise RuntimeError("unavailable")
        if failure_at == "open":
            raise OSError("unavailable")
        return BrokenReader()

    monkeypatch.setattr(kanban_worker_node, "open_replica", open_reader)
    (_, parts), = score.rank(local, [row], now_ms=9_120_000, weights={})
    assert (parts.effective_priority, parts.descendants, parts.age_buckets) == (400, 0, 2)
    assert local.in_transaction  # do not commit the caller's pending writes


def test_dispatch_order_uses_ready_episode_not_creation(sources, monkeypatch):
    import time
    from hermes_cli.kanban_dispatch_priority import order_with_remote_waiters

    local, remote = sources
    returning = add(remote, "returning", 1000)
    waiting = add(remote, "waiting", 8500)
    remote.execute("INSERT INTO task_events VALUES ('returning', 'promoted', '{}', 9000)")
    monkeypatch.setattr(time, "time", lambda: 9120)
    monkeypatch.setattr(score, "configured_weights", lambda: dict(score.DEFAULT_WEIGHTS))
    assert [r["id"] for r in order_with_remote_waiters(local, [returning, waiting])] == ["waiting", "returning"]


def test_source_snapshot_pins_history_and_preserves_caller_transaction(sources):
    local, _ = sources
    add(local, "local", 9000)
    seen = []
    local.set_trace_callback(seen.append)
    score.rank(local, [{"id": "local"}], now_ms=9_120_000, weights={})
    local.set_trace_callback(None)
    assert "SAVEPOINT admission_score_read" in seen
    assert "RELEASE admission_score_read" in seen
    assert local.in_transaction
    local.rollback()
    assert not local.execute("SELECT * FROM tasks").fetchall()


def test_creation_priority_excludes_scoring_bonuses_and_obeys_local_unlink(sources):
    local, remote = sources
    add(local, "owner", 1, 300, "urgent", 8)
    add(remote, "waiter", 1, 60000)
    remote.execute("INSERT INTO task_links VALUES ('owner', 'waiter')")
    assert score.effective_priorities(local, ["owner", "unknown"]) == {"owner": 1000}
    add(local, "waiter", 1, 60000)
    assert score.effective_priorities(local, ["owner"]) == {"owner": 300}
    assert local.execute("SELECT priority FROM tasks WHERE id='owner'").fetchone()[0] == 300


def test_snapshot_runtime_bug_is_not_misclassified_as_unavailable(sources, monkeypatch):
    local, _ = sources
    row = add(local, "local", 9000, 400)

    original = score._source_snapshot

    def broken_snapshot(conn, ids, **kwargs):
        if conn is not local:
            raise RuntimeError("implementation bug")
        return original(conn, ids, **kwargs)

    monkeypatch.setattr(score, "_source_snapshot", broken_snapshot)
    with pytest.raises(RuntimeError, match="implementation bug"):
        score.rank(local, [row], now_ms=9_120_000, weights={})


def test_missing_replica_keeps_local_scoring(sources, monkeypatch):
    from hermes_cli import kanban_worker_node

    local, _ = sources
    row = add(local, "local", 9000, 400)
    monkeypatch.setattr(kanban_worker_node, "open_replica", lambda conn: None)
    (_, parts), = score.rank(local, [row], now_ms=9_120_000, weights={})
    assert (parts.effective_priority, parts.age_buckets) == (400, 2)
