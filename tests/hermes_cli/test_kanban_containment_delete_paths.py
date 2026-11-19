"""Released containment eviction/purge and held import regression paths."""
from __future__ import annotations

import logging
import sqlite3

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_containment_service as service
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_containment_connections import held
from tests.hermes_cli import test_kanban_containment_lifecycle as lc
from tests.hermes_cli import test_kanban_containment_remote as rm

board = rm.board


def _as_foreign_copy(conn, tid):
    conn.execute("UPDATE tasks SET created_on='home' WHERE id=?", (tid,))
    conn.execute("DELETE FROM kanban_sync_outbox WHERE row_uid=? OR json_extract(fields,'$.task_id')=?", (tid, tid))
    conn.commit()


def _evict(conn, monkeypatch, caplog):
    from hermes_cli import kanban_replica_lag as lag
    monkeypatch.setattr(wn, "_guard_held", lambda c, ids: set())
    monkeypatch.setattr(lag, "held_copies", lambda c, path, ids: set())
    monkeypatch.setattr(wn, "_replica_path", lambda c: None)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=wn._log.name):
        gone = wn.evict(conn)
    failed = [r.getMessage() for r in caplog.records if "could not evict" in r.getMessage()]
    return gone, failed


def _child(conn, relay):
    return conn.execute("SELECT child_id FROM task_links WHERE parent_id=?", (relay,)).fetchone()[0]


def _released_child(conn, monkeypatch):
    relay, authority, session, _hub = rm.setup(conn, monkeypatch)
    child = _child(conn, relay)
    rm.adopt(conn, monkeypatch, relay, session)
    with scoped_current_session_id(session):
        stop = service.contain(conn, task_id=child, authority=authority, summary="Fixture")
    rm.adopt(conn, monkeypatch, authority, session)
    with scoped_current_session_id(session):
        assert service.resume(conn, task_id=child, episode=stop["episode"])["status"] == "ready"
    assert not held(conn, child)
    return child


@pytest.mark.parametrize("released", [False, True])
def test_evict_foreign_copy(board, monkeypatch, caplog, released):
    if released:
        child = _released_child(board, monkeypatch)
    else:
        relay, _authority, _session, _hub = rm.setup(board, monkeypatch)
        child = _child(board, relay)
    _as_foreign_copy(board, child)
    gone, failed = _evict(board, monkeypatch, caplog)
    assert child in gone and not failed
    assert kb.get_task(board, child) is None
    assert _evict(board, monkeypatch, caplog) == ([], [])


@pytest.mark.parametrize("released", [False, True])
def test_synced_purge(board, monkeypatch, released):
    from hermes_cli.kanban_db_archive import archive_task
    if released:
        child = _released_child(board, monkeypatch)
    else:
        relay, _authority, _session, _hub = rm.setup(board, monkeypatch)
        child = _child(board, relay)
    assert archive_task(board, child)
    assert kb.delete_archived_task(board, child)
    assert kb.get_task(board, child) is None


def test_import_board_preserves_hold(board, tmp_path):
    from hermes_cli import kanban_transfer as transfer
    relay, _consumer, _authority, target, spec = lc.setup(board)
    lc.contain(board, relay, target, spec)
    tid = target["task_id"]
    assert held(board, tid)
    out = transfer.export_board("default", tmp_path / "out.tar.gz")
    res = transfer.import_board(str(out if not isinstance(out, dict) else tmp_path / "out.tar.gz"), slug="imported-fixture")
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing(board=res["board"]) as conn:
        assert held(conn, tid)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
