"""The sync thread's own repair-revision steps are credited; everyone else's still repair. [daveey]

Hub, 2026-11-12 (t_01a1d02bf3e0d019): with #1461 the hub still ran the full repair pass on almost every round, because
its own push acknowledgements, remote-parent mirror and pool reservations moved the live repair revision after
the round's inputs read, and only feed applies were credited. ``kanban_sync_own_writes`` records those steps with a
connection-local TEMP trigger gated to the sync thread; a write from any other connection or thread is still a gap.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_own_writes as own
from hermes_cli import kanban_sync_repair_window as window
from tests.hermes_cli import test_kanban_sync_feed_apply_credit as credit

kanban_home = credit.kanban_home
svc = credit.svc
clock = credit.clock
live = credit.live


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "x.db"
    a = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    a.executescript("""
        CREATE TABLE tasks(id TEXT PRIMARY KEY, v INT);
        CREATE TABLE kanban_repair_revision(id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL);
        INSERT INTO kanban_repair_revision VALUES (1, 0);
        CREATE TRIGGER kanban_repair_tasks_insert AFTER INSERT ON tasks
            BEGIN UPDATE kanban_repair_revision SET revision = revision + 1 WHERE id = 1; END;
        CREATE TRIGGER kanban_repair_tasks_update AFTER UPDATE ON tasks
            BEGIN UPDATE kanban_repair_revision SET revision = revision + 1 WHERE id = 1; END;
    """)
    b = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    own.reset()
    yield a, b
    own.reset()
    a.close()
    b.close()


def test_only_this_threads_committed_steps_on_this_connection_are_recorded(db):
    a, b = db
    schema = a.execute("PRAGMA schema_version").fetchone()[0]
    with own.credited(a):
        a.execute("INSERT INTO tasks VALUES ('t1', 1)")                       # 0 -> 1 credited
        a.execute("BEGIN IMMEDIATE")
        a.execute("UPDATE tasks SET v = 2")                                    # rolled back: never credited
        a.execute("ROLLBACK")
        b.execute("INSERT INTO tasks VALUES ('t2', 1)")                       # 1 -> 2 another connection
        other = threading.Thread(target=lambda: a.execute("UPDATE tasks SET v = 3"))  # 2 -> 4: other thread
        other.start()
        other.join()
        a.execute("UPDATE tasks SET v = 4")                                    # 4 -> 6 credited
    a.execute("UPDATE tasks SET v = 5")                                        # 6 -> 8 after the block
    assert own.take(b) == [(0, 1), (4, 5), (5, 6)], "steps are per file, so any connection to it takes them"
    assert own.take(a) == []
    assert a.execute("PRAGMA schema_version").fetchone()[0] == schema, "a TEMP recorder must not move the schema"
    assert a.execute("SELECT revision FROM kanban_repair_revision").fetchone()[0] == 8


def test_a_block_that_ends_inside_a_transaction_keeps_its_steps_until_it_commits(db):
    a, _b = db
    a.execute("BEGIN IMMEDIATE")
    with own.credited(a):
        a.execute("INSERT INTO tasks VALUES ('t1', 1)")
    assert own.take(a) == [], "uncommitted: not credited yet"
    a.execute("COMMIT")
    with own.credited(a):
        pass
    assert own.take(a) == [(0, 1)]


def test_a_board_without_the_revision_table_records_nothing(tmp_path):
    conn = sqlite3.connect(tmp_path / "bare.db", isolation_level=None)
    conn.execute("CREATE TABLE tasks(id TEXT)")
    with own.credited(conn):
        conn.execute("INSERT INTO tasks VALUES ('t')")
    assert own.take(conn) == []
    conn.close()


# --- the round ---------------------------------------------------------------------------------------------


def test_a_reservation_write_by_the_sync_thread_is_credited(live, svc, kanban_home, monkeypatch, clock):
    """``reevaluate`` runs after the inputs read (pool reservations, the feed pull after them)."""
    import hermes_cli.kanban_worker_placement as placement
    parent, child = credit._settle(live, svc, kanban_home)
    calls = credit._spy(monkeypatch)

    def reserve(conn, *a, **k):
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = priority + 1 WHERE id = ?", (child,))
    monkeypatch.setattr(placement, "reevaluate", reserve)
    for _ in range(3):
        clock[0] += 1
        credit._heartbeat(svc, parent)
        assert credit._round(kanban_home, svc).repair_deferred
    assert not any(calls.values()), calls


def test_another_connections_write_during_the_round_still_repairs(live, svc, kanban_home, monkeypatch, clock):
    """A dispatcher/heartbeat write on another connection while the sync thread is inside a credited block is
    not the sync thread's: a gap, so the next round repairs and finds it."""
    parent, child = credit._settle(live, svc, kanban_home)
    calls = credit._spy(monkeypatch)
    real_push = ksc.push
    done = []

    def push_and_race(conn, *a, **k):
        # On the round's send after its inputs read (it also sends before its pulls, t_01a1d36ddcea59fe).
        if not done and not k.get("before_pull"):
            done.append(1)
            credit._uncaptured_edit(live, child, "raced")
        return real_push(conn, *a, **k)
    monkeypatch.setattr(ksc, "push", push_and_race)
    credit._heartbeat(svc, parent)
    credit._round(kanban_home, svc)
    clock[0] += 1
    credit._heartbeat(svc, parent)
    assert not credit._round(kanban_home, svc).repair_deferred
    assert calls["repair_divergence"] == 1, calls
    credit._round(kanban_home, svc)
    assert credit._board_title(svc, child) == "raced"


# --- the counter ---------------------------------------------------------------------------------------------


def test_round_outcomes_are_counted_and_logged_per_board(live, svc, kanban_home, monkeypatch, clock, caplog):
    parent, child = credit._settle(live, svc, kanban_home)
    window._ROUNDS.clear()
    window._ROUNDS_SINCE.clear()
    credit._heartbeat(svc, parent)
    credit._round(kanban_home, svc)                       # deferred
    credit._round(kanban_home, svc)                       # still deferred (the window's pass is not done yet)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET title = 'x' WHERE id = ?", (child,))
    clock[0] += 1
    credit._round(kanban_home, svc)                       # local_write
    assert window.round_counts(credit.BOARD) == {"deferred": 2, "local_write": 1}
    clock[0] += window.ROUND_LOG_S
    with caplog.at_level(logging.INFO, logger="hermes_cli.kanban_sync_client"):
        credit._round(kanban_home, svc)
    lines = [r.getMessage() for r in caplog.records if "repair rounds" in r.getMessage()]
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"kanban sync: {credit.BOARD} repair rounds in ")
    assert "local_write 1" in lines[0] and " deferred" in lines[0], lines[0]
    assert window.round_counts(credit.BOARD) == {}, "the log line starts a new period"


def test_decide_reasons():
    window.reset()
    inputs = ((1, (5, 7)), (1, 2, 0), "primary")
    assert window.decide("k", None, [], 0.0, 30.0) == "unknown"
    assert window.decide("k", inputs, [], 0.0, 30.0) == "first"
    window._REPAIRED["k"] = inputs
    window._REPAIRED_AT["k"] = 0.0
    moved = ((1, (6, 7)), (1, 2, 1), "primary")
    assert window.decide("k", moved, None, 1.0, 30.0) == "rebuilt"
    assert window.decide("k", ((1, (6, 8)), (1, 2, 1), "primary"), [(5, 6)], 1.0, 30.0) == "inputs"
    assert window.decide("k", moved, [], 1.0, 30.0) == "local_write"
    assert window.decide("k", moved, [(5, 6)], 31.0, 30.0) == "window"
    assert window.decide("k", moved, [(5, 6)], 1.0, 30.0) == "deferred"
    window.reset()
