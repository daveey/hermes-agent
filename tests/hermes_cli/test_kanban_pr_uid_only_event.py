"""Portable UID-only system comments must not poison unrelated PR provenance."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_pr_replica_comment_uid as helpers

kanban_home = helpers.kanban_home
open_pr = helpers.open_pr
pytestmark = pytest.mark.usefixtures("kanban_home", "open_pr")


def _uid_only(conn, tid, cid, uid):
    helpers._replicate_with_uid(conn, tid, cid, uid, len_delta=0)
    for eid, payload in conn.execute(
        "SELECT id,payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,)
    ).fetchall():
        data = json.loads(payload)
        if data.get("comment_id") == cid:
            del data["comment_id"]
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), eid))


@pytest.mark.parametrize("uid", ["portable-system-note", "", None])
def test_historical_uid_only_event_does_not_poison_wake(monkeypatch, uid):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, _ = helpers._owner_parked(conn, monkeypatch, now)
        cid = kb.add_comment(conn, tid, "kanban-spend-reviewer", "historical system decision")
        _uid_only(conn, tid, cid, uid)
        kb.add_comment(conn, tid, "kanban-dispatcher", f"Woken: the job finished: succeeded ({helpers.URL}).")
        expected = None if uid else "active_pr"
        assert prs.active_pr_reason(conn, tid, now + 700) == expected


@pytest.mark.parametrize("fault", [
    "missing-row", "duplicate-uid", "malformed-id", "bad-author", "bad-length",
    "list-uid", "dict-uid", "numeric-uid",
])
def test_uid_only_unreadable_provenance_stays_closed(monkeypatch, fault):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, _ = helpers._owner_parked(conn, monkeypatch, now)
        cid = kb.add_comment(conn, tid, "system", "historical decision")
        _uid_only(conn, tid, cid, "system-uid")
        eid, payload = conn.execute(
            "SELECT id,payload FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        data = json.loads(payload)
        overrides = {
            "missing-row": {"comment_uid": "unknown"},
            "malformed-id": {"comment_id": False},
            "bad-author": {"author": None},
            "bad-length": {"len": "19"},
            "duplicate-uid": {},
            "list-uid": {"comment_uid": ["system-uid"]},
            "dict-uid": {"comment_uid": {"system-uid": True}},
            "numeric-uid": {"comment_uid": 7},
        }
        data.update(overrides[fault])
        if fault == "duplicate-uid":
            conn.execute("UPDATE task_comments SET uid='system-uid' WHERE task_id=?", (tid,))
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), eid))
        kb.add_comment(conn, tid, "kanban-dispatcher", f"Woken: the job finished: succeeded ({helpers.URL}).")
        assert prs._comment_events(conn, tid) is None
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"


def test_uid_only_own_publication_still_holds(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with kbc.connect() as conn:
        tid, run_id = helpers._owner_parked(conn, monkeypatch, now)
        cid = helpers._worker_comment(conn, tid, run_id, monkeypatch, "Opened https://github.com/example/project/pull/99")
        _uid_only(conn, tid, cid, "portable-owner-note")
        assert prs.active_pr_reason(conn, tid, now + 700) == "active_pr"
        assert prs._local_comment_id(conn, tid, cid) is None
