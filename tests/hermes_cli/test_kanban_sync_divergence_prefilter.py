"""The divergence repair compares settled cards in SQLite, not row by row in Python. [daveey]

Hub py-spy 2026-11-13 04:17Z (t_01a1d2cc49634a78): after the chat-link fix landed, 100% of the sync-loop thread
sat in ``repair_divergence`` fetching every task row of both files (``SELECT *``, ~5.5k cards). Each fetched row
waits for the contended gateway GIL, so one pass held the loop 40+ s and the replica went 11+ min stale.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_divergence as div
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "macbook", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def lease_client():
    yield from support.lease_client()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def _settle(live, kanban_home, svc, cards):
    sync.set_mode(live, "primary")
    ids = [kb.create_task(live, title=f"card {i}", body=f"body {i}", assignee="worker") for i in range(cards)]
    for _ in range(3):
        ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    return ids


def _uncaptured(live, sql, args):
    with kbc.write_txn(live):
        sync.drop_triggers(live)  # an uncaptured write: only the repair scan finds it
        live.execute(sql, args)
    sync.install_triggers(live)


def _spy_repair(monkeypatch):
    real, results = ksc.repair_divergence, []

    def spied(live_conn, shadow, gateway):
        got = real(live_conn, shadow, gateway)
        results.append(got)
        return got

    monkeypatch.setattr(ksc, "repair_divergence", spied)
    return results


def test_settled_cards_never_reach_python(live, kanban_home, monkeypatch):
    """Only differing cards are materialized: base fetched and converted every settled card (2 x 60 calls)."""
    svc = FakeService()
    ids = _settle(live, kanban_home, svc, 60)
    converted = []
    real_fields = ksc._row_fields

    def counted(conn, table, row):
        converted.append(row["id"])
        return real_fields(conn, table, row)

    monkeypatch.setattr(ksc, "_row_fields", counted)
    results = _spy_repair(monkeypatch)
    _uncaptured(live, "UPDATE tasks SET title = 'gap edit' WHERE id = ?", (ids[7],))
    ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert results and results[0] == {ids[7]: ["title"]}, results
    assert set(converted) <= {ids[7]}, f"{len(converted)} rows converted for 1 differing card of 60"
    assert svc.board(BOARD)["rows"]["tasks"][ids[7]]["title"] == "gap edit"


@pytest.mark.parametrize("prefilter", [True, False])
@pytest.mark.parametrize("sql, field", [
    ("UPDATE tasks SET title = 'gap edit' WHERE id = ?", "title"),
    ("UPDATE tasks SET body = 'gap body' WHERE id = ?", "body"),
    ("UPDATE tasks SET priority = 9 WHERE id = ?", "priority"),
    ("UPDATE tasks SET result = 'gap result' WHERE id = ?", "result"),
])
def test_prefilter_and_full_scan_queue_the_same_fields(live, kanban_home, monkeypatch, prefilter, sql, field):
    svc = FakeService()
    ids = _settle(live, kanban_home, svc, 5)
    if not prefilter:
        monkeypatch.setattr(div, "_differing_settled", lambda *_a: None)
    results = _spy_repair(monkeypatch)
    _uncaptured(live, sql, (ids[2],))
    # A local-only column (never synced) must not make a card a candidate in either path.
    _uncaptured(live, "UPDATE tasks SET workspace_path = '/elsewhere' WHERE id = ?", (ids[3],))
    ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    assert results and results[0] == {ids[2]: [field]}, results


def test_prefilter_skips_unsettled_and_matches_run_refs(live, kanban_home):
    """A card with a newer local version is not settled; equal run uids under different local ids are equal."""
    svc = FakeService()
    ids = _settle(live, kanban_home, svc, 3)
    replica = ksc.open_shadow(BOARD, ME)
    try:
        assert div._differing_settled(live, replica, ME) == set()
        live.execute("UPDATE tasks SET sync_version = sync_version + 100 WHERE id = ?", (ids[0],))
        _uncaptured(live, "UPDATE tasks SET title = 'x' WHERE id = ?", (ids[0],))
        assert div._differing_settled(live, replica, ME) == set()
        _uncaptured(live, "UPDATE tasks SET title = 'y' WHERE id = ?", (ids[1],))
        assert div._differing_settled(live, replica, ME) == {ids[1]}
    finally:
        replica.close()


def test_prefilter_compares_run_refs_by_uid(live, kanban_home, lease_client):
    """``current_run_id`` is a per-file local id: the pre-filter compares the run uids it names (review of PR #1882).
    Equal uids under different local ids are no difference; the same local id naming another uid is one."""
    svc = FakeService()
    ids = _settle(live, kanban_home, svc, 3)
    for tid in ids[:2]:
        assert kb.claim_task(live, tid) is not None
    for _ in range(3):
        ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)
    replica = ksc.open_shadow(BOARD, ME)
    try:
        runs = {tid: (live.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0],
                      replica.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
                                      "WHERE t.id = ?", (tid,)).fetchone())
                for tid in ids[:2]}
        assert all(run_id is not None and uid is not None for run_id, uid in runs.values()), runs
        assert div._differing_settled(live, replica, ME) == set()
        # ids[0]: the same run uid under another local id (raw ids now differ; the uids do not).
        run_id = runs[ids[0]][0]
        _uncaptured(live, "UPDATE task_runs SET id = id + 1000 WHERE id = ?", (run_id,))
        _uncaptured(live, "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id + 1000, ids[0]))
        assert live.execute("SELECT current_run_id FROM tasks WHERE id = ?", (ids[0],)).fetchone()[0] != \
            replica.execute("SELECT current_run_id FROM tasks WHERE id = ?", (ids[0],)).fetchone()[0]
        assert div._differing_settled(live, replica, ME) == set()
        # ids[1]: the same local id now names another run uid (only the uid differs).
        _uncaptured(live, "UPDATE task_runs SET uid = 'other-run-uid' WHERE id = ?", (runs[ids[1]][0],))
        assert div._differing_settled(live, replica, ME) == {ids[1]}
    finally:
        replica.close()


def test_prefilter_unavailable_for_in_memory_files(live):
    import sqlite3
    mem = sqlite3.connect(":memory:")
    try:
        assert div._differing_settled(live, mem, ME) is None
    finally:
        mem.close()
