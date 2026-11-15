"""The notice-guarantee candidate scan is driven by the (few) kanban-notice rows, not by every session. [daveey]

On a 1.3M-message hub the per-session "last active message" probe ran for every one of 23k sessions each pass
(0.56% of a core idle, H2 follow-up). The indexed form starts from ``idx_messages_kanban_notice`` and returns
exactly the rows of the legacy form.
"""
from __future__ import annotations

import time

import pytest

import hermes_state_outcome as so
from hermes_state import SessionDB
from hermes_state_common import _sql_session_last_active
from tui_gateway import notice_guarantee as ng

LEGACY = ng._CANDIDATES_SQL


@pytest.fixture
def db(tmp_path):
    so._reset_hooks_for_tests()
    handle = SessionDB(tmp_path / "state.db")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def _notice(db, sid, text="n"):
    return db.append_kanban_notice(sid, text, {"notice_id": f"{sid}:{text}", "display_text": text})


def _rows(db, sql):
    sql = sql.format(last_active=_sql_session_last_active("s"))
    return sorted((tuple(r) for r in db._read_all(sql)), key=lambda r: r[0])


def _populate(db):
    for sid in ("tail", "answered", "archived", "muted", "compressed", "moved", "inactive", "tool_tail", "two"):
        db.create_session(sid, "desktop")
        db.append_message(sid, "user", f"hello {sid}")
    _notice(db, "tail")
    _notice(db, "answered")
    db.append_message("answered", "assistant", "handled")
    for sid in ("archived", "muted", "compressed", "moved"):
        _notice(db, sid)
    db._write_rowcount("UPDATE sessions SET archived = 1 WHERE id = 'archived'")
    db._write_rowcount("UPDATE sessions SET muted = 1 WHERE id = 'muted'")
    db._write_rowcount("UPDATE sessions SET end_reason = 'compression' WHERE id = 'compressed'")
    db._write_rowcount("UPDATE sessions SET end_reason = 'moved_to_gateway:hub' WHERE id = 'moved'")
    row = _notice(db, "inactive")
    db._write_rowcount("UPDATE messages SET active = 0 WHERE id = ?", (row,))
    _notice(db, "tool_tail")
    db.append_message("tool_tail", "tool", "result")
    _notice(db, "two", "first")
    _notice(db, "two", "second")


def test_indexed_candidates_equal_the_legacy_scan(db):
    _populate(db)
    expected = _rows(db, LEGACY)
    assert {r[0] for r in expected} == {"tail", "two"}
    assert _rows(db, ng.candidates_sql(db)) == expected


def test_indexed_candidates_start_from_the_notice_index(db):
    _populate(db)
    sql = ng.candidates_sql(db).format(last_active=_sql_session_last_active("s"))
    plan = " | ".join(str(r[-1]) for r in db._read_all("EXPLAIN QUERY PLAN " + sql))
    assert "idx_messages_kanban_notice" in plan
    assert "SCAN s" not in plan and "SCAN sessions" not in plan


def test_without_the_index_the_legacy_form_is_used(db):
    _populate(db)
    db._write_rowcount("DROP INDEX idx_messages_kanban_notice")
    assert ng.candidates_sql(db) == LEGACY
    got = ng.candidates(db, time.time() + ng.GRACE_S + 1)
    assert {c["sid"] for c in got} == {"tail", "two"}
