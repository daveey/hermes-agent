"""Replica cards: a synced comment row id differs from its event's ``comment_id``. [daveey]

A card homed on another board (the MacBook) and run on this host keeps the home board's
``commented`` events verbatim, so ``comment_id`` is the HOME row id while the local
``task_comments`` row was inserted under a new id (2026-10-30: home 6684 -> hub row 9723).
The respawn guard must match events to rows by (created_at, author, len), not by raw id.
"""
import json
import time

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")
URL = "https://github.com/example/project/pull/1523"
OFFSET = 1000


@pytest.fixture(autouse=True)
def open_pr(monkeypatch):
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _clock(monkeypatch, t):
    monkeypatch.setattr(time, "time", lambda: t)


def _replicate(conn, tid):
    """Renumber the card's comment rows as a replica sync does (new local ids, empty uid)."""
    conn.execute("UPDATE task_comments SET id = id + ? WHERE task_id = ?", (OFFSET, tid))
    if any(c[1] == "uid" for c in conn.execute("PRAGMA table_info(task_comments)")):  # sync-only column
        conn.execute("UPDATE task_comments SET uid = '' WHERE task_id = ?", (tid,))


def _ran_card(conn):
    tid = kb.create_task(conn, title="recover PR", assignee="a")
    kb.claim_task(conn, tid)
    return tid


def _chat_fyi(conn, tid, body=f"FYI: wake to recover {URL}"):
    return kb.add_comment(conn, tid, "default", body)  # no worker env: no writer_run_id


def test_replica_chat_fyi_naming_open_pr_is_not_held(monkeypatch):
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        _clock(monkeypatch, now + 5)
        cid = _chat_fyi(conn, tid)
        _replicate(conn, tid)
        local = conn.execute("SELECT id FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
        assert local != cid
        assert prs.active_pr_reason(conn, tid, now + 10) is None


def test_replica_chat_fyi_sharing_its_second_with_unblocked_is_not_held(monkeypatch):
    """The t_01a18c47420df7e2 shape: the strict-after handoff check cannot lift it."""
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        _clock(monkeypatch, now + 5)
        _chat_fyi(conn, tid)
        kb._append_event(conn, tid, "unblocked", {"reason": "resume"})
        _replicate(conn, tid)
        assert prs.active_pr_reason(conn, tid, now + 10) is None


def test_replica_own_worker_pr_comment_still_held(monkeypatch):
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        _clock(monkeypatch, now + 5)
        worker_comment(conn, tid, "default", f"Opened {URL}")
        _replicate(conn, tid)
        assert prs.active_pr_reason(conn, tid, now + 10) == "active_pr"


@pytest.mark.parametrize("replica", [False, True])
def test_two_rows_with_one_triple_fail_closed(monkeypatch, replica):
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        _clock(monkeypatch, now + 5)
        _chat_fyi(conn, tid)
        _chat_fyi(conn, tid)  # same (created_at, author, len): rows are indistinguishable
        if replica:
            _replicate(conn, tid)
        assert prs.active_pr_reason(conn, tid, now + 10) == "active_pr"


def test_two_events_matching_one_row_fail_closed(monkeypatch):
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        _clock(monkeypatch, now + 5)
        body = f"FYI: wake to recover {URL}"
        _chat_fyi(conn, tid, body)
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'commented', ?, ?)",
            (tid, json.dumps({"author": "default", "len": len(body), "comment_id": 424242}), now + 5),
        )
        _replicate(conn, tid)
        assert prs.active_pr_reason(conn, tid, now + 10) == "active_pr"


def test_home_board_event_written_seconds_after_its_row_still_matches(monkeypatch):
    """add_comment stamps the row before taking the write lock; its event can be later."""
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        _clock(monkeypatch, now + 5)
        _chat_fyi(conn, tid)
        conn.execute("UPDATE task_events SET created_at = created_at + 30 "
                     "WHERE task_id = ? AND kind = 'commented'", (tid,))
        assert prs.active_pr_reason(conn, tid, now + 60) is None


def _immediate_checkpoint(monkeypatch, conn, now):
    tid = _ran_card(conn)
    worker_comment(conn, tid, "default", f"Draft {URL}")
    assert kc.continue_task(conn, tid, checkpoint="finish the existing PR")["ok"]
    return tid


def _latest_pr_row(conn, tid):
    return conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]


def test_replica_checkpoint_watermark_maps_to_local_row(monkeypatch):
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _immediate_checkpoint(monkeypatch, conn, now)
        _replicate(conn, tid)  # watermark keeps the home id; the PR row is now home id + OFFSET
        assert prs._immediate_checkpoint_after(conn, tid, now, _latest_pr_row(conn, tid))
        assert prs.active_pr_reason(conn, tid, now + 1) is None


@pytest.mark.parametrize("later,expected", [(False, "active_pr"), (True, None)])
def test_unmappable_watermark_uses_checkpoint_time(monkeypatch, later, expected):
    now = int(time.time())
    _clock(monkeypatch, now)
    with kbc.connect() as conn:
        tid = _ran_card(conn)
        worker_comment(conn, tid, "default", f"Draft {URL}")
        _clock(monkeypatch, now + (1 if later else 0))
        assert kc.continue_task(conn, tid, checkpoint="finish the existing PR")["ok"]
        row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='continued'",
                           (tid,)).fetchone()
        data = kb._json_dict(row["payload"])
        data["comment_id_at_checkpoint"] = 0  # names no comment event
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(data), row["id"]))
        _replicate(conn, tid)
        got = prs._immediate_checkpoint_after(conn, tid, now, _latest_pr_row(conn, tid))
        assert got is (expected is None)
