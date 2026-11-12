"""Legacy dropped asks (drop event, no ``resolved_check_dropped_at``) are marked once by reconcile."""
import json

import pytest

from hermes_cli import kanban_ask_resolved as kar
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_ask_resolved_check as helpers

board = helpers.board
DROPS = [kar.BROKEN_EVENT, kar.LOST_EVENT]


def _request(conn, tid):
    value = kb.get_task(conn, tid).human_request
    return (json.loads(value) if isinstance(value, str) else value) or {}


def _legacy(conn, tid, kind, at=5000, **payload):
    """Old core: probe row gone, drop event appended, ask left unmarked."""
    with kb.write_txn(conn):
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
        kb._append_event(conn, tid, kind, {"cmd": "exit 1", **payload})
        conn.execute("UPDATE task_events SET created_at = ? WHERE id = (SELECT MAX(id) FROM task_events "
                     "WHERE task_id = ?)", (at, tid))


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]


def _snapshot(conn):
    return (conn.total_changes, _count(conn), conn.execute("SELECT human_request FROM tasks").fetchall())


@pytest.mark.parametrize("kind", DROPS)
def test_old_drop_is_marked_once_with_one_reemitted_event(board, kind):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        _legacy(conn, tid, kind, at=5000)
        events = _count(conn)
        original = max(kb.list_events(conn, tid), key=lambda e: e.id)
        out = kar.reconcile(conn, now=99999)
        assert out["restored"] == out["lost"] == []
        req = _request(conn, tid)
        assert (req["resolved_check_dropped_at"], req["resolved_check_drop_reason"]) == (5000, kind)
        assert req["resolved_check"] == "exit 1"
        assert _count(conn) == events + 1 and kwc.get_state(conn, tid) is None
        emitted = max(kb.list_events(conn, tid), key=lambda e: e.id)
        assert emitted.kind == kind
        reference = {"origin_event_uid": original.uid} if original.uid else {"origin_event_id": original.id}
        assert emitted.payload == {"backfilled": True, **reference}
        assert kb.get_task(conn, tid).status == "blocked"
        before = _snapshot(conn)
        assert not any(kar.reconcile(conn, now=199999).values())
        assert _snapshot(conn) == before  # no writes at all


def test_refile_does_not_inherit_old_mark_and_old_drop_does_not_mark_refile(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        _legacy(conn, tid, kar.BROKEN_EVENT)
        kar.reconcile(conn, now=9000)
        marked = _request(conn, tid)
        assert marked["resolved_check_dropped_at"] == 5000
        with kb.write_txn(conn):
            kar.store_request(conn, tid, marked)
            kb._append_event(conn, tid, "blocked", {})
        refiled = _request(conn, tid)
        assert "resolved_check_dropped_at" not in refiled and kwc.get_state(conn, tid) is not None
        # Refiled with its probe row lost (legacy): the old drop precedes the new filing, so no mark.
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (tid,))
        kar.reconcile(conn, now=10000)
        assert "resolved_check_dropped_at" not in _request(conn, tid)
        assert kar.RESTORED_EVENT in {e.kind for e in kb.list_events(conn, tid)}


def test_resolved_event_is_not_marked(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        _legacy(conn, tid, kar.BROKEN_EVENT)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, kar.EVENT, {"cmd": "exit 1", "rc": 0})
        kar.reconcile(conn, now=9000)
        assert "resolved_check_dropped_at" not in _request(conn, tid)


def test_latest_drop_kind_wins(board):
    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        _legacy(conn, tid, kar.BROKEN_EVENT, at=5000)
        _legacy(conn, tid, kar.LOST_EVENT, at=6000)
        kar.reconcile(conn, now=9000)
        assert (_request(conn, tid)["resolved_check_dropped_at"],
                _request(conn, tid)["resolved_check_drop_reason"]) == (6000, kar.LOST_EVENT)


def test_remote_probe_owner_and_live_probe_and_unblocked_are_left_alone(board, monkeypatch):
    from hermes_cli.kanban_ask_resolved_backfill import backfill

    with kbc.connect_closing() as conn:
        live = helpers._blocked_ask(conn, "exit 1")  # probe row still present
        with kb.write_txn(conn):
            kb._append_event(conn, live, kar.BROKEN_EVENT, {})
        remote = helpers._blocked_ask(conn, "exit 1")
        with kb.write_txn(conn):
            kb._append_event(conn, remote, "blocked", {"probe_host": "gw-worker"})
        _legacy(conn, remote, kar.BROKEN_EVENT)
        done = helpers._blocked_ask(conn, "exit 1")
        _legacy(conn, done, kar.BROKEN_EVENT)
        kb.unblock_task(conn, done)
        monkeypatch.setattr(kwc, "own_gateway", lambda c: "gw-home")
        assert backfill(conn) == []
        for tid in (live, remote, done):
            assert "resolved_check_dropped_at" not in _request(conn, tid)
        monkeypatch.setattr(kwc, "own_gateway", lambda c: "gw-worker")  # the owner itself marks it
        assert backfill(conn) == [remote]


def test_write_fence_rechecks_latest_filing_and_missing_probe(board, monkeypatch):
    from hermes_cli import kanban_ask_resolved_backfill as bf

    with kbc.connect_closing() as conn:
        tid = helpers._blocked_ask(conn, "exit 1")
        _legacy(conn, tid, kar.BROKEN_EVENT)
        real = bf._latest_drop

        def racing(c, t):
            result = real(c, t)
            if not racing.done:  # between the read pass and the txn, the ask is refiled
                racing.done = True
                with kb.write_txn(c):
                    kar.store_request(c, t, _request(c, t))
                    kb._append_event(c, t, "blocked", {})
            return result

        racing.done = False
        monkeypatch.setattr(bf, "_latest_drop", racing)
        assert bf.backfill(conn) == []
        assert "resolved_check_dropped_at" not in _request(conn, tid)
        assert kwc.get_state(conn, tid) is not None
