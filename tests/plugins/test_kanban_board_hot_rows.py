"""GET /board hot rows: a refresh after a commit steps few SQLite rows into Python.

Hub profile (2026-11-22): a 9.3k-event card gaining one event re-read all 9.3k rows, every
displayed card's runs were signed row by row, and GET /boards waited on the observer lock for
whole board builds. Each test pins the same payload as a cold read and the reduced work.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_diagnostic_history import EventHistory
from plugins.kanban.dashboard import board_event_history as beh
from plugins.kanban.dashboard import board_read as br
from plugins.kanban.dashboard import board_run_history as brh
from plugins.kanban.dashboard import board_rows
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board

needs_ordered = pytest.mark.skipif(not beh.ORDERED_AGGREGATES, reason="SQLite < 3.44: full reloads kept")
HISTORY = 300


def _write(sql_args):
    conn = kbc.connect()
    try:
        for sql, args in sql_args:
            conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _events(tid, kind, n, created_at=5):
    return [("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, '{}', ?)",
             (tid, kind, created_at))] * n


@pytest.fixture
def long_card(board):
    ids, _run = helpers._seed()
    tid = ids[1]
    _write(_events(tid, "dispatch_skipped", HISTORY))
    helpers._read(board)  # FULL: no seals yet
    _write(_events(tid, "dispatch_skipped", 1))
    helpers._read(board)  # first delta reloads the moved card once and seals it
    return board, tid


@pytest.fixture
def spy(monkeypatch):
    seen = {"reloaded": [], "appended": []}
    real_rows, real_extended = br._rows_by_task, EventHistory.extended.__func__

    def rows_by_task(conn, table, ids):
        if table == "task_events":
            seen["reloaded"].extend(ids)
        return real_rows(conn, table, ids)

    def extended(cls, history, rows):
        seen["appended"].append(len(rows))
        return real_extended(cls, history, rows)

    monkeypatch.setattr(br, "_rows_by_task", rows_by_task)
    monkeypatch.setattr(EventHistory, "extended", classmethod(extended))
    return seen


def _warm(mod):
    return helpers._stable(helpers._read(mod)), dict(board_rows._entries)


def _cold(mod, saved):
    """A cold read of the same database; the warm retained rows are put back afterwards."""
    cold = helpers._cold(mod)
    board_rows._entries.update(saved)
    return cold


@needs_ordered
@pytest.mark.parametrize("kind", ["dispatch_skipped", "heartbeat"])
def test_grown_history_reads_only_its_new_rows(long_card, spy, kind):
    mod, tid = long_card
    _write(_events(tid, kind, 2, created_at=7))
    warm, saved = _warm(mod)
    assert tid not in spy["reloaded"], "a grown history must not be re-read whole"
    assert spy["appended"] == [beh.RECENT + 2], "only the newest retained rows are read again"
    assert warm == _cold(mod, saved)


@needs_ordered
def test_a_late_stamp_on_the_newest_event_is_read_again(long_card, spy):
    """Sync stamps ``origin_gateway`` on a fresh event after its insert (seen on every busy hub
    card): the newest rows are re-read, never vouched for, so the stamp shows without a reload."""
    mod, tid = long_card
    _write([("UPDATE task_events SET origin_gateway = 'hub' WHERE id = "
             "(SELECT MAX(id) FROM task_events WHERE task_id = ?)", (tid,))])
    warm, saved = _warm(mod)
    assert tid not in spy["reloaded"]
    assert spy["appended"] == [beh.RECENT]
    assert warm == _cold(mod, saved)


def _traced_read(mod):
    import unittest.mock

    from hermes_cli import kanban_generation
    statements, original = [], kanban_generation.open_ro

    def observe(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with unittest.mock.patch.object(kanban_generation, "open_ro", observe):
        warm, saved = _warm(mod)
    scans = [s for s in statements if s.startswith("SELECT * FROM task_events WHERE")]
    return warm, saved, statements, scans


@needs_ordered
def test_unchanged_marked_history_skips_the_exact_rescan(long_card):
    mod, tid = long_card
    _write([("UPDATE tasks SET title = title || ' edited' WHERE id = ?", (tid,))])
    warm, saved, statements, scans = _traced_read(mod)
    assert any(tid in s for s in statements if "group_concat" in s), "the marked card is compared in SQL"
    assert scans == [], scans
    assert warm == _cold(mod, saved)


@needs_ordered
def test_a_history_proven_equal_is_sealed_for_the_next_mark(board):
    """A card marked again and again without event changes (title or bookkeeping edits) pays the
    exact rescan once, then is compared in SQL."""
    ids, _run = helpers._seed()
    tid = ids[2]
    _write(_events(tid, "commented", HISTORY))
    helpers._read(board)  # FULL: nothing sealed
    for i, expect_scan in ((1, True), (2, False), (3, False)):
        _write([("UPDATE tasks SET title = ? WHERE id = ?", (f"edit {i}", tid))])
        warm, saved, _statements, scans = _traced_read(board)
        assert bool(scans) == expect_scan, (i, scans)
        assert warm == _cold(board, saved)


@needs_ordered
@pytest.mark.parametrize("change", ["update_old", "delete_old", "backfill", "delete_then_append"])
def test_any_change_below_the_retained_tail_reloads_exactly(long_card, spy, change):
    mod, tid = long_card
    first = f"(SELECT MIN(id) FROM task_events WHERE task_id = '{tid}')"
    sql = {
        "update_old": [(f"UPDATE task_events SET payload = '{{\"x\":1}}' WHERE id = {first}", ())],
        "delete_old": [(f"DELETE FROM task_events WHERE id = {first}", ())],
        "backfill": [(f"INSERT INTO task_events (id, task_id, kind, payload, created_at) "
                      f"VALUES ({first} - 1, ?, 'heartbeat', '{{}}', 1)", (tid,))],
        "delete_then_append": [(f"DELETE FROM task_events WHERE id = {first}", ()),
                               *_events(tid, "dispatch_skipped", 1)],
    }[change]
    if change == "backfill":  # make room below the card's first event
        _write([("UPDATE task_events SET id = id + 1000000 WHERE task_id = ?", (tid,))])
        helpers._read(mod)
        spy["reloaded"].clear()
    _write(sql)
    warm, saved = _warm(mod)
    assert tid in spy["reloaded"]
    assert warm == _cold(mod, saved)


def test_row_text_is_injective_and_never_vouches_for_a_real_zero():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE t (k INTEGER PRIMARY KEY, v)")
        values = [None, 0, 1, 1.0, 0.5, "1", b"1", "", b"", "a\x00b", "a", -1, 2 ** 62, 1e300]
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(v,) for v in values])
        sql = f"SELECT {beh.row_text(('v',))} FROM t ORDER BY k"
        texts = [r[0] for r in conn.execute(sql)]
        assert len(set(texts)) == len(values)
        assert not any("," in t for t in texts)
        conn.execute("INSERT INTO t (v) VALUES (-0.0)")
        zero = f"SELECT {beh.row_text(('v',))} FROM t WHERE k = (SELECT MAX(k) FROM t)"
        assert conn.execute(zero).fetchone()[0] != conn.execute(zero).fetchone()[0]


@needs_ordered
def test_outside_a_transaction_nothing_is_sealed():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT)")
        conn.execute("INSERT INTO task_events VALUES (1, 't', 'created')")
        conn.commit()
        assert not conn.in_transaction
        assert beh.extend(conn, {"t": (EventHistory(()), None)}) == ({}, {})


@pytest.fixture
def runs_source():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, claim_expires, worker_pid)")
    conn.executemany("INSERT INTO task_runs (task_id, claim_expires, worker_pid) VALUES (?, ?, ?)",
                     [(f"t{i % 3}", i, None) for i in range(60)])
    yield conn
    conn.close()


@needs_ordered
def test_run_signatures_return_one_row_per_task(runs_source):
    statements = []
    runs_source.set_trace_callback(statements.append)
    sigs = brh._signatures(runs_source, ["t0", "t1", "t2", "none"], ["claim_expires", "worker_pid"])
    assert sorted(sigs) == ["t0", "t1", "t2"]  # 3 rows for 60 runs; a task without runs is absent
    assert len(statements) == 1 and "GROUP BY task_id" in statements[0]
    for value in (1.0, "20", b"20", 0.0):
        runs_source.execute("UPDATE task_runs SET claim_expires = ? WHERE id = 21", (value,))
        again = brh._signatures(runs_source, ["t2"], ["claim_expires", "worker_pid"])
        assert again["t2"] != sigs["t2"], value
    runs_source.execute("UPDATE task_runs SET claim_expires = 20 WHERE id = 21")
    assert brh._signatures(runs_source, ["t2"], ["claim_expires", "worker_pid"]) == {"t2": sigs["t2"]}


def test_count_polls_do_not_wait_for_a_board_build(board):
    """``version``/``content_version`` borrow the observer only for PRAGMAs; a build holds the
    separate refresh lock, which still serializes concurrent builds of the same board."""
    helpers._seed()
    path = str(kb.kanban_db_path())
    assert board_rows.version(path) is not None
    entered, release = threading.Event(), threading.Event()
    order = []

    def slow_build(conn, prev, changed):
        order.append("build")
        entered.set()
        assert release.wait(10)
        return {"rows": 1}

    def run(name, build):
        board_rows.incremental((path, name), path, build, lambda: pytest.fail("fallback"))

    builder = threading.Thread(target=run, args=("a", slow_build))
    builder.start()
    assert entered.wait(10)
    try:
        poll = threading.Thread(target=lambda: order.append(board_rows.content_version(path)))
        poll.start()
        poll.join(5)
        assert not poll.is_alive(), "a count poll waited for a board build"
        second = threading.Thread(target=run, args=("b", lambda *a: order.append("second") or {}))
        second.start()
        second.join(0.3)
        assert second.is_alive(), "builds of one board must stay serialized"
    finally:
        release.set()
    builder.join(10)
    second.join(10)
    assert order[0] == "build" and order[-1] == "second"
