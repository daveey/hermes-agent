"""Conditional clear observations must not hide a due chat-wait notice."""
import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_parent_unclaimed as pu
from tests.hermes_cli import test_kanban_ready_wait_checkpoint as base

conn = base.conn


@pytest.mark.parametrize("kind", ["shared_blocker_cleared", "continued"])
@pytest.mark.parametrize("status", [None, "review", "ready"])
def test_conditional_event_preserves_due_notice_unless_ready(conn, monkeypatch, kind, status):
    t0 = 1795470116
    monkeypatch.setattr("time.time", lambda: t0)
    tid = kb.create_task(conn, title="already ready subscribed card", assignee="default")
    base._subscribe(conn, tid)
    now = t0 + cw.THRESHOLD_SECONDS
    monkeypatch.setattr("time.time", lambda: now)
    payload = {"status": status} if status is not None else {}
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, payload)
    since = now if status == "ready" else t0
    assert pu.ready_since(conn, tid, t0) == since
    assert cw.sweep(conn, now=now) == (0 if status == "ready" else 1)
    assert cw.sweep(conn, now=now + cw.THRESHOLD_SECONDS) == (1 if status == "ready" else 0)
