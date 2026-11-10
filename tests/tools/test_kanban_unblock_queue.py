"""A card a chat unblocks on David's live approval is ranked, sized and watched. [daveey]

Defect agent-70dc07d1a5 (2026-11-02): David said "go" in a live chat; it unblocked t_01a19e4974912dcf (a
``human_request`` card) and told him "a worker will pick it up". The card sat ready and unclaimed for 113 min
behind 47 older p0 cards: the unblock kept its p0 rank, its result was only ``{"status": "ready"}`` (no queue
place), and nothing told the chat that the card it waited on directly had no worker. These tests pin:
priority >= 10 on a chat's human-approval unblock (``priority_raised`` reason ``human_go``), no change for a worker
or a plain block, ``queue`` plus the don't-promise hint in the result, and ONE ``chat_wait_unclaimed`` notice per
ready episode for a chat-subscribed card ready and unclaimed 30+ min.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kn
from hermes_cli.kanban_db_dispatch_queries import lane_rows

_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_WORKER_TASK")


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _connect():
    return kbc.connect()


def _ready(conn, title, *, priority=0, created_at=None):
    tid = kb.create_task(conn, title=title, assignee="default", priority=priority, initial_status="running")
    if created_at is not None:
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, tid))
        conn.commit()
    return tid


def _human_blocked(conn, title="hire: send offer", *, priority=0, with_request=True):
    tid = kb.create_task(conn, title=title, assignee="default", priority=priority)
    kb.block_task(conn, tid, reason="needs David's go")
    if with_request:
        conn.execute("UPDATE tasks SET human_request=? WHERE id=?",
                     (json.dumps({"action": "Reply go in this chat. Unblock = send; archive = drop."}), tid))
        conn.commit()
    return tid


def _unblock(tid):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_unblock({"task_id": tid}))


def _behind_eight_older_p0(conn):
    base = int(time.time()) - 7200
    return [_ready(conn, f"older p0 {i}", created_at=base + i) for i in range(8)]


def _events(conn, tid, kind):
    return [json.loads(p) if p else {} for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


# --- priority on a chat's human-approval unblock ----------------------------------------------------------------


def test_chat_unblock_of_human_request_card_raises_priority_and_jumps_the_queue(board):
    conn = _connect()
    older = _behind_eight_older_p0(conn)
    tid = _human_blocked(conn)
    conn.close()

    out = _unblock(tid)
    assert out["ok"] is True and out["status"] == "ready", out

    conn = _connect()
    try:
        assert kb.get_task(conn, tid).priority == 10
        raised = _events(conn, tid, "priority_raised")
        assert len(raised) == 1 and raised[0]["reason"] == "human_go"
        assert raised[0]["from"] == 0 and raised[0]["to"] == 10
        order = [r["id"] for r in lane_rows(conn, "ready")]
        assert order[0] == tid and set(order[1:]) == set(older)
    finally:
        conn.close()


def test_chat_unblock_never_lowers_a_higher_priority(board):
    conn = _connect()
    tid = _human_blocked(conn, priority=20)
    conn.close()
    _unblock(tid)
    conn = _connect()
    try:
        assert kb.get_task(conn, tid).priority == 20
        assert _events(conn, tid, "priority_raised") == []
    finally:
        conn.close()


def test_chat_unblock_without_human_request_keeps_priority(board):
    conn = _connect()
    tid = _human_blocked(conn, with_request=False)
    conn.close()
    _unblock(tid)
    conn = _connect()
    try:
        assert kb.get_task(conn, tid).priority == 0
        assert _events(conn, tid, "priority_raised") == []
    finally:
        conn.close()


def test_worker_unblock_keeps_priority(board, monkeypatch):
    """A self-heal fix worker requeueing its named card is not David's go: no raise."""
    conn = _connect()
    target = _human_blocked(conn)
    fix = kb.create_task(conn, title=f"self-heal fix: kanban card {target} on hermes-hub/default failed 3x (crashed):",
                         created_by="self-heal", idempotency_key="selfheal:kanban-50a03854e8:fix",
                         assignee="default")
    kb.claim_task(conn, fix)
    monkeypatch.setenv("HERMES_KANBAN_TASK", fix)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, fix)))
    conn.close()

    out = _unblock(target)
    assert out.get("ok") is True and out.get("via") == "self-heal-requeue", out
    conn = _connect()
    try:
        assert kb.get_task(conn, target).priority == 0
        assert _events(conn, target, "priority_raised") == []
    finally:
        conn.close()


# --- queue place in the unblock result ------------------------------------------------------------------------


def test_unblock_result_carries_queue_and_the_no_promise_hint_when_the_queue_is_full(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "resolve_max_in_progress", lambda *_a, **_k: 2)
    conn = _connect()
    base = int(time.time()) - 7200
    for i in range(3):
        _ready(conn, f"p50 {i}", priority=50, created_at=base + i)
    tid = _human_blocked(conn)
    conn.close()

    out = _unblock(tid)
    queue = out.get("queue") or {}
    assert queue.get("lane") == "local", out
    assert queue.get("ahead") == 3 and queue.get("limit") == 2
    hint = out.get("queue_hint") or ""
    assert "don't promise a pickup" in hint
    assert "bump priority" in hint and "re-place" in hint and "this chat" in hint


def test_unblock_result_has_queue_but_no_hint_when_a_slot_is_near(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "resolve_max_in_progress", lambda *_a, **_k: 7)
    conn = _connect()
    tid = _human_blocked(conn)
    conn.close()
    out = _unblock(tid)
    assert (out.get("queue") or {}).get("ahead") == 0, out
    assert "queue_hint" not in out


# --- direct chat wait notice -----------------------------------------------------------------------------------


def _subscribe(conn, tid, chat="origin-chat"):
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat, delivery_metadata={"reason": "create"})


def _age_ready(conn, tid, seconds):
    then = int(time.time()) - seconds
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (then, tid))
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (then, tid))
    conn.commit()


def _notices(conn, tid):
    from hermes_cli import kanban_chat_wait as cw
    return _events(conn, tid, cw.NOTICE_KIND)


def test_subscribed_card_ready_31_min_gets_one_notice(board):
    from hermes_cli import kanban_chat_wait as cw
    conn = _connect()
    try:
        tid = _ready(conn, "hire: send offer")
        _subscribe(conn, tid)
        _age_ready(conn, tid, 31 * 60)
        assert cw.sweep(conn) == 1
        notices = _notices(conn, tid)
        assert len(notices) == 1
        text = cw.notice_text(notices[0])
        assert tid in text and "bump" in text and "re-place" in text and "here" in text
        assert cw.sweep(conn) == 0
        assert len(_notices(conn, tid)) == 1
    finally:
        conn.close()


def test_claimed_card_sends_no_notice(board):
    from hermes_cli import kanban_chat_wait as cw
    conn = _connect()
    try:
        tid = _ready(conn, "claimed")
        _subscribe(conn, tid)
        _age_ready(conn, tid, 31 * 60)
        conn.execute("UPDATE tasks SET claim_lock='hub:1' WHERE id=?", (tid,))
        conn.commit()
        assert cw.sweep(conn) == 0
        assert _notices(conn, tid) == []
    finally:
        conn.close()


def test_short_wait_or_no_chat_sends_no_notice(board):
    from hermes_cli import kanban_chat_wait as cw
    conn = _connect()
    try:
        fresh = _ready(conn, "fresh")
        _subscribe(conn, fresh)
        _age_ready(conn, fresh, 10 * 60)
        orphan = _ready(conn, "no chat")
        _age_ready(conn, orphan, 31 * 60)
        assert cw.sweep(conn) == 0
    finally:
        conn.close()


def test_a_new_ready_episode_notifies_again(board):
    from hermes_cli import kanban_chat_wait as cw
    conn = _connect()
    try:
        tid = _ready(conn, "re-queued")
        _subscribe(conn, tid)
        _age_ready(conn, tid, 3 * 3600)
        assert cw.sweep(conn) == 1
        kb.block_task(conn, tid, reason="again")
        kb.unblock_task(conn, tid)
        then = int(time.time()) - 31 * 60
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind IN ('blocked','unblocked')",
                     (then, tid))
        conn.commit()
        assert cw.sweep(conn) == 1
        assert len(_notices(conn, tid)) == 2
    finally:
        conn.close()


def test_notice_kind_is_delivered_and_wakes_the_chat():
    from gateway import kanban_watchers_notifier as gw
    from hermes_cli import kanban_chat_wait as cw
    from tui_gateway import session_notifications as sn
    assert cw.NOTICE_KIND in gw.TERMINAL_KINDS and cw.NOTICE_KIND in gw._WAKE_KINDS
    assert cw.NOTICE_KIND in gw._EVENT_FORMATTERS
    assert cw.NOTICE_KIND in sn._KANBAN_NOTIFY_KINDS and cw.NOTICE_KIND in sn._KANBAN_EVENT_FORMATTERS


def test_dispatch_tick_runs_the_chat_wait_sweep(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    conn = _connect()
    try:
        tid = _ready(conn, "waited on by a chat")
        _subscribe(conn, tid)
        _age_ready(conn, tid, 31 * 60)
        monkeypatch.setattr(kbd, "_tick_spawn_budget", lambda *a, **k: (False, 0))
        kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a, **k: None)
        assert len(_notices(conn, tid)) == 1
    finally:
        conn.close()
