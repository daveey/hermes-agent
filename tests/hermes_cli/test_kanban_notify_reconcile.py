"""Read-only hub+origin evidence followed by transactional subscription retirement."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as notify
from hermes_cli.kanban_notify_reconcile import reconcile_refused_sub
from hermes_cli.kanban_session_activity import _unseen_on
from hermes_state import SessionDB

CHAT = "s_origin"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    with kbc.connect_closing() as conn, SessionDB(tmp_path / "origin.db") as db:
        tid = kb.create_task(conn, title="result", assignee="default")
        db.create_session(CHAT, "desktop")
        notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT,
                              notifier_profile="default", delivery_metadata={"reason": "send_refused"})
        notify.add_notify_sub(conn, task_id=tid, platform="hub", chat_id=f"remote/default/{CHAT}")
        with kb.write_txn(conn):
            eid = kb._append_event(conn, tid, "completed", {"summary": "result"})
        eid = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        key = f"remote:source:default/default/{tid}:{eid}"
        args = dict(task_id=tid, chat_id=CHAT, source_gateway="source", source_profile="default", board="default",
                    local_session_exists=lambda p, s: False,
                    owner=lambda p, s: ("remote", p) if db.get_session(s) else None,
                    delivered_keys=lambda g, p, s, keys: db.kanban_notice_keys_delivered(s, keys))
        yield conn, db, tid, key, args


def receipt(db, key):
    return db.record_kanban_notice(CHAT, "result", {"notice_id": key, "kanban_event_keys": [key]})


def test_receipt_retires_only_phantom_without_redelivery(world):
    conn, db, tid, key, args = world
    assert _unseen_on(conn) == {CHAT: 1}
    assert reconcile_refused_sub(conn, **args) == "missing_receipt"
    first, inserted = receipt(db, key)
    assert inserted
    assert reconcile_refused_sub(conn, **args) == "reconciled"
    assert _unseen_on(conn) == {}
    assert reconcile_refused_sub(conn, **args) == "absent"
    assert receipt(db, key) == (first, False)
    assert len(db.get_messages(CHAT)) == 1
    assert [(r["platform"], r["chat_id"]) for r in notify.list_notify_subs(conn, tid)] == [("hub", f"remote/default/{CHAT}")]


def _fail(*_a):
    raise OSError("offline")


_CASES = {
    "local": ("local_owner", {"local_session_exists": lambda p, s: True}),
    "unknown": ("unknown_owner", {"owner": lambda p, s: None}),
    "unreadable": ("evidence_unavailable", {"delivered_keys": _fail}),
    "owner_unreadable": ("evidence_unavailable", {"owner": _fail}),
    "wrong_profile": ("unknown_owner", {"owner": lambda p, s: ("remote", "other")}),
    "no_receipt": ("missing_receipt", {}),
}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_unknown_or_undelivered_remains_unseen_and_retryable(world, case):
    conn, db, _tid, _key, args = world
    expected, overrides = _CASES[case]
    args.update(overrides)
    assert reconcile_refused_sub(conn, **args) == expected
    assert _unseen_on(conn) == {CHAT: 1}
    assert db.get_messages(CHAT) == []


def test_racing_completion_does_not_get_discarded(world):
    conn, db, tid, key, args = world
    receipt(db, key)
    def raced(g, p, s, keys):
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "completed", {"summary": "another result"})
        return db.kanban_notice_keys_delivered(s, keys)
    args["delivered_keys"] = raced
    assert reconcile_refused_sub(conn, **args) == "changed_retry"
    assert _unseen_on(conn) == {CHAT: 1}


def test_earlier_event_text_is_not_receipt(world):
    conn, db, _tid, key, args = world
    receipt(db, key + "-earlier")
    assert reconcile_refused_sub(conn, **args) == "missing_receipt"
    assert _unseen_on(conn) == {CHAT: 1}


def test_receipt_in_compacted_ancestor_counts_once(world):
    """The origin compressed the chat after the notice landed: the receipt lives on the compacted parent,
    the owner reads it through lineage from the tip, and a re-delivery attempt still dedupes."""
    conn, db, _tid, key, args = world
    first, inserted = receipt(db, key)
    assert inserted
    db.end_session(CHAT, "compression")
    db.create_session("s_tip", "desktop", parent_session_id=CHAT)
    args["delivered_keys"] = lambda g, p, s, keys: db.kanban_notice_keys_delivered("s_tip", keys)
    assert reconcile_refused_sub(conn, **args) == "reconciled"
    assert _unseen_on(conn) == {}
    again, inserted = db.record_kanban_notice("s_tip", "result", {"notice_id": key, "kanban_event_keys": [key]})
    assert (again, inserted) == (first, False)
    notices = [m for sid in (CHAT, "s_tip") for m in db.get_messages(sid)]
    assert len(notices) == 1


def test_drained_row_is_left_alone(world):
    conn, db, tid, key, args = world
    receipt(db, key)
    eid = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_notify_subs SET last_event_id=? WHERE task_id=? AND platform='tui'", (eid, tid))
    assert reconcile_refused_sub(conn, **args) == "nothing_pending"
    assert len(notify.list_notify_subs(conn, tid, platform="tui")) == 1


def test_genuine_local_row_is_not_reconciled_and_stays_unseen(world):
    """A refused send into a chat on THIS gateway is real local work: never retired, still counted."""
    conn, db, _tid, key, args = world
    receipt(db, key)
    args["local_session_exists"] = lambda p, s: True
    assert reconcile_refused_sub(conn, **args) == "local_owner"
    assert _unseen_on(conn) == {CHAT: 1}


def test_missing_hub_route_keeps_row(world):
    conn, db, tid, key, args = world
    receipt(db, key)
    notify.remove_notify_sub(conn, task_id=tid, platform="hub", chat_id=f"remote/default/{CHAT}")
    assert reconcile_refused_sub(conn, **args) == "missing_remote_route"
    assert _unseen_on(conn) == {CHAT: 1}


def test_row_without_refusal_provenance_is_untouched(world):
    conn, db, tid, key, args = world
    receipt(db, key)
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_notify_subs SET delivery_metadata=NULL WHERE task_id=? AND platform='tui'", (tid,))
    assert reconcile_refused_sub(conn, **args) == "provenance_mismatch"
    assert _unseen_on(conn) == {CHAT: 1}
