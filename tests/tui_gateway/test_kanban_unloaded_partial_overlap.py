"""The local unloaded sweeper re-renders a claim range that a chat's lineage already partly holds. [daveey]

A live poller wrote the row for the card's ``blocked`` event and died before its ack; the card was then unblocked
and completed. The sweeper claims the whole cursor range ``(old, new]``. Since #1164 the writer refuses a row whose
keys only partly overlap the lineage (``KanbanNoticeOverlapError``), so without a pre-filter every pass rewound and
logged a traceback and the done notice waited until the chat was opened (t_01a1ce5a5b6f9bb3).
"""

import collections

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tests.tui_gateway import test_kanban_unloaded_sweep as sweep
from tui_gateway import kanban_unloaded as ku

db = sweep.db
_row_path_only = sweep._row_path_only
CHAT, LATER = sweep.CHAT, sweep.LATER


def _events(tid, kind):
    conn = kbc.connect()
    try:
        return [e for e in kb.list_events(conn, tid) if e.kind == kind]
    finally:
        conn.close()


def _blocked_then_done(seed):
    """A tui-subscribed card that blocks, gets a live row for its ``blocked`` event (``seed``), then completes."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="partly delivered card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT)
        assert kb.block_task(conn, tid, reason="registry is down")
    finally:
        conn.close()
    seed(tid)
    conn = kbc.connect()
    try:
        assert kb.unblock_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="shipped after all")
    finally:
        conn.close()
    return tid


def _keys(db):
    return [k for m in sweep._notices(db) for k in (m["display_metadata"] or {}).get("kanban_event_keys", [])]


def _last_event_id(tid):
    conn = kbc.connect()
    try:
        return max(int(e.id) for e in kb.list_events(conn, tid))
    finally:
        conn.close()


def test_partly_delivered_range_writes_only_the_missing_notice(db, caplog):
    def seed(tid):
        (blocked,) = _events(tid, "blocked")
        db.append_kanban_notice(CHAT, "blocked notice", {"notice_id": "live:x",
                                                         "kanban_event_keys": [f"default:{int(blocked.id)}"]})

    tid = _blocked_then_done(seed)
    (completed,) = _events(tid, "completed")
    assert len(sweep._notices(db)) == 1

    out = ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db))

    assert [d["task_id"] for d in out] == [tid]
    notices = sweep._notices(db)
    assert len(notices) == 2
    new = notices[-1]
    assert new["display_metadata"]["kanban_event_keys"] == [f"default:{int(completed.id)}"]
    assert "shipped after all" in new["content"] and "registry is down" not in new["content"]
    assert sweep._cursor(tid) == _last_event_id(tid)
    assert "kanban unloaded sweep failed" not in caplog.text

    assert ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db)) == []
    assert len(sweep._notices(db)) == 2
    assert [k for k, n in collections.Counter(_keys(db)).items() if n > 1] == []


def test_fully_delivered_range_is_acked_without_a_row(db):
    def seed(tid):
        pass

    tid = _blocked_then_done(seed)
    keys = [f"default:{int(e.id)}" for kind in ("blocked", "completed") for e in _events(tid, kind)]
    db.append_kanban_notice(CHAT, "both notices", {"notice_id": "live:y", "kanban_event_keys": keys})

    assert ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db)) == []

    assert len(sweep._notices(db)) == 1
    assert sweep._cursor(tid) == _last_event_id(tid)
    assert ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db)) == []
    assert len(sweep._notices(db)) == 1


def test_failed_key_read_offers_the_whole_range_and_the_writer_still_guards_it(db, monkeypatch):
    """The filter is an optimization over the writer's atomic recheck: an unreadable lineage keeps the old
    behavior (the overlap is refused, the claim rewinds) and the next pass delivers."""
    import sqlite3

    def seed(tid):
        (blocked,) = _events(tid, "blocked")
        db.append_kanban_notice(CHAT, "blocked notice", {"notice_id": "live:x",
                                                         "kanban_event_keys": [f"default:{int(blocked.id)}"]})

    tid = _blocked_then_done(seed)
    before = sweep._cursor(tid)
    with monkeypatch.context() as m:
        m.setattr(db, "kanban_notice_keys_delivered",
                  lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        assert ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db)) == []
        assert sweep._cursor(tid) == before and len(sweep._notices(db)) == 1
    assert len(ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db))) == 1
    assert len(sweep._notices(db)) == 2 and sweep._cursor(tid) == _last_event_id(tid)


def test_race_after_the_filter_rewinds_and_retries(db, monkeypatch, caplog):
    """A live poller lands the blocked notice between the filter's read and the write: the writer's atomic recheck
    refuses the now partly covered batch, the claim rewinds, and the next pass delivers only the done notice."""
    tid = _blocked_then_done(lambda tid: None)
    (blocked,) = _events(tid, "blocked")
    (completed,) = _events(tid, "completed")
    before = sweep._cursor(tid)
    real = db.kanban_notice_keys_delivered

    def stale_read(tip, keys):
        got = real(tip, keys)
        monkeypatch.setattr(db, "kanban_notice_keys_delivered", real)
        db.append_kanban_notice(CHAT, "racer", {"notice_id": "live:z",
                                                "kanban_event_keys": [f"default:{int(blocked.id)}"]})
        return got

    monkeypatch.setattr(db, "kanban_notice_keys_delivered", stale_read)
    assert ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db)) == []
    assert sweep._cursor(tid) == before and len(sweep._notices(db)) == 1
    assert "KanbanNoticeOverlapError" in caplog.text

    assert [d["task_id"] for d in ku.sweep_once(set(), now=LATER, session_db=sweep._session_db(db))] == [tid]
    notices = sweep._notices(db)
    assert len(notices) == 2
    assert notices[-1]["display_metadata"]["kanban_event_keys"] == [f"default:{int(completed.id)}"]
    assert sweep._cursor(tid) == _last_event_id(tid)
    assert [k for k, n in collections.Counter(_keys(db)).items() if n > 1] == []
