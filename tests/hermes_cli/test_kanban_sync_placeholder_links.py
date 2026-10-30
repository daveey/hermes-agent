"""Placement-attribution chat links (``kanban-placement*`` sessions) leave the Board with the live file.

Production (hub, 2026-10-30): 238 released placement links stayed on the Board after the live rows were deleted
locally with no capture trigger, a shadow-only ``task_chat_links`` mismatch at pending=0 that blocked a clean diff.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "macbook", "default"
PLACEMENT = "kanban-placement-macbook"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _diff(live):
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        return ksc.diff(live, shadow, ME)
    finally:
        shadow.close()


def _board_live(svc, uid):
    row = svc.board(BOARD)["rows"].get("task_chat_links", {}).get(uid)
    return row is not None and not row.get("deleted_at")


def _insert_link(live, tid, session, *, applying=False):
    """A chat link written the way the pre-filter reconcile wrote placement links (captured unless ``applying``)."""
    uid = links.link_uid(tid, ME, None, session, "created")
    with kbc.write_txn(live):
        if applying:
            live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES (?, ?, ?, NULL, ?, 'created', 0, 1)", (uid, tid, ME, session))
        if applying:
            live.execute("UPDATE kanban_sync_state SET applying = 0")
    return uid


def _uncaptured_delete(live, uid):
    """The production state: the live row vanished with no capture, so the Board kept it."""
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("DELETE FROM task_chat_links WHERE uid = ?", (uid,))
        live.execute("UPDATE kanban_sync_state SET applying = 0")


def _settle(kanban_home, svc, n=3):
    for _ in range(n):
        _round(kanban_home, svc)


def test_a_local_chat_link_delete_is_captured_and_reaches_the_board(live, svc, kanban_home):
    tid = kb.create_task(live, title="watched", assignee="worker", session_id="s_creator")
    uid = _insert_link(live, tid, "s_watcher")
    _settle(kanban_home, svc)
    assert _board_live(svc, uid)
    with kbc.write_txn(live):
        live.execute("DELETE FROM task_chat_links WHERE uid = ?", (uid,))
    ops = [(r[0], r[1]) for r in live.execute(
        "SELECT op, row_uid FROM kanban_sync_outbox WHERE table_name = 'task_chat_links' AND state = 'pending'")]
    assert ("delete", uid) in ops
    assert live.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name = 'task_chat_links' AND row_uid = ?",
                        (uid,)).fetchone()
    _settle(kanban_home, svc)
    assert not _board_live(svc, uid)
    assert _diff(live)["child_mismatch"] == {}


def test_reconcile_deletes_a_placement_link_and_the_board_drops_it(live, svc, kanban_home):
    tid = kb.create_task(live, title="placed", assignee="worker", session_id=PLACEMENT)
    uid = _insert_link(live, tid, PLACEMENT)  # what reconcile wrote before placeholder sessions were filtered
    _settle(kanban_home, svc)
    assert _board_live(svc, uid)
    counts = links.reconcile(live, BOARD, ME, ledger=False)
    assert counts["retired"] == 1 and counts["released"] == 0
    assert not live.execute("SELECT 1 FROM task_chat_links WHERE uid = ?", (uid,)).fetchone()
    _settle(kanban_home, svc)
    assert not _board_live(svc, uid)
    d = _diff(live)
    assert d["child_mismatch"] == {} and d["pending"] == 0
    assert links.reconcile(live, BOARD, ME, ledger=False) == {"added": 0, "changed": 0, "released": 0, "retired": 0}


def test_a_placement_link_lost_from_live_without_capture_is_deleted_from_the_board(live, svc, kanban_home, caplog):
    tid = kb.create_task(live, title="placed", assignee="worker", session_id=PLACEMENT)
    uid = _insert_link(live, tid, PLACEMENT)
    _settle(kanban_home, svc)
    _uncaptured_delete(live, uid)
    shadow = kbc.connect(ksc.shadow_path(BOARD))
    try:
        assert ksc.diff(live, shadow, ME)["child_mismatch"] == {"task_chat_links": 1}  # the production symptom
    finally:
        shadow.close()
    _settle(kanban_home, svc)
    assert not _board_live(svc, uid)
    assert "placement chat links absent from live" in caplog.text
    d = _diff(live)
    assert d["child_mismatch"] == {} and d["pending"] == 0 and d["clean"]
    queued = sum("placement chat links absent from live" in r.message for r in caplog.records)
    _settle(kanban_home, svc)
    assert sum("placement chat links absent from live" in r.message for r in caplog.records) == queued  # once


def test_a_real_chat_link_lost_from_live_stays_a_reported_mismatch(live, svc, kanban_home):
    tid = kb.create_task(live, title="watched", assignee="worker", session_id="s_creator")
    uid = _insert_link(live, tid, "s_real_chat")
    _settle(kanban_home, svc)
    _uncaptured_delete(live, uid)
    _settle(kanban_home, svc)
    assert _board_live(svc, uid)  # never deleted on the replica's say-so
    assert not [r for r in live.execute("SELECT op FROM kanban_sync_outbox WHERE table_name = 'task_chat_links' "
                                        "AND op = 'delete'")]
    assert _diff(live)["child_mismatch"] == {"task_chat_links": 1}


def test_a_delete_under_applying_is_not_captured(live):
    assert "kanban_sync_task_chat_links_del" in sync.wanted_triggers(live)
    tid = kb.create_task(live, title="copy", assignee="worker")
    uid = _insert_link(live, tid, "s_chat", applying=True)
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("DELETE FROM task_chat_links WHERE uid = ?", (uid,))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'task_chat_links'").fetchone()
