"""reclaim_stale_running_handoffs must tell failure apart from "nothing to reclaim".

The gateway's startup reclaim used to get ``[]`` both when no row was stranded and when the write lock was
unavailable, so a reclaim that hit a busy state.db was never retried (agent-9cbeb4e676).
"""

import sqlite3

from hermes_state import SessionDB


def _strand(db, sid):
    db.create_session(sid, "cli")
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET handoff_state = 'running' WHERE id = ?", (sid,)))


def test_reclaim_returns_none_when_write_lock_is_held(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _strand(db, "dead")
    db._WRITE_PATIENCE_S = 0.3  # instance override keeps the lock wait short

    holder = sqlite3.connect(str(tmp_path / "state.db"), timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        result = db.reclaim_stale_running_handoffs("gateway died")
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    assert result is None, f"a failed reclaim must return None, not {result!r}"
    assert db.get_handoff_state("dead")["state"] == "running"

    assert db.reclaim_stale_running_handoffs("gateway died") == ["dead"]
    assert db.get_handoff_state("dead")["state"] == "failed"


def test_reclaim_with_nothing_stranded_returns_empty_list(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("ok", "cli")
    assert db.reclaim_stale_running_handoffs("gateway died") == []
