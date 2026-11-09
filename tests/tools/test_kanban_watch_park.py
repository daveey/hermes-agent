"""A ``watch`` park: a later check that needs nothing from the chat does not hold it open. [daveey]

A chat created a card and parked it 69 h out ("check the DoorDash sign-in on Nov 5") while telling David
nothing was needed. Scheduled cards count as the chat's open work, so the chat stayed unfinished for days,
and ``kanban_complete`` on the parked card failed with a vague "stale run" (self-heal kanban-85e428ecb2).

``kanban_schedule(watch=true)`` parks the card as a watch: while it waits it is not the chat's open work
(counts 0, the close-out gate sees no task), and its notify subscription stays, so a later block or done
still reaches the chat. A long park without ``watch`` says which chat it holds and until when.
"""
from __future__ import annotations

import json
import time

import pytest

CHAT = "20261102_205456_6898ba"
HUMAN_REQUEST = {
    "action": "Sign in to the site again in Chrome. Unblock = re-check; archive = drop the check.",
    "why_human": "Only the account owner can complete the sign-in.",
    "interaction": "physical",
    "scope": "One sign-in",
    "evidence": "The renewed credential was rejected.",
}


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_KEY"):
        monkeypatch.delenv(var, raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _chat_card(kb) -> str:
    """A card the chat created, with the chat's ``tui`` create subscription (what kanban_create writes)."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="check sign-in after expiry", assignee="default", session_id=CHAT)
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT,
                           delivery_metadata={"reason": "create"})
    return tid


def _wake_iso(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + hours * 3600))


def _schedule(tid: str, hours: float, ref: str = "credential expiry", **extra) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_schedule({
        "task_id": tid, "reason": f"SCHEDULED_UNTIL={_wake_iso(hours)} verify the credential renewed ({ref})",
        "clock_only": True, "awaiting": {"kind": "clock", "ref": ref}, **extra}))


def _open_for_chat(kb) -> tuple[int, list]:
    from hermes_cli import kanban_session_activity as ksa
    path = kb.kanban_db_path()
    counts = ksa._read_counts(path)
    tasks = ksa._read_session_tasks(path, [CHAT], ksa.ACTIVE_STATUSES)
    return counts.get(CHAT, 0), tasks


def test_watch_park_frees_the_chat_and_keeps_the_notice(board):
    tid = _chat_card(board)
    assert _open_for_chat(board)[0] == 1  # unparked: the chat's own work
    out = _schedule(tid, 72, watch=True)
    assert out.get("ok") is not False and not out.get("error"), out
    assert out["status"] == "scheduled"
    assert out.get("watch") is True
    n, tasks = _open_for_chat(board)
    assert n == 0, "a watch park must not hold the chat open"
    assert tasks == [], "the close-out gate (session_tasks ACTIVE_STATUSES) must see no open card"
    from hermes_cli import kanban_session_activity as ksa
    path = board.kanban_db_path()
    assert ksa._read_parked(path)[0].get(CHAT, 0) == 0
    # The subscription stays. When the watch wakes it is the chat's work again, and a block reaches the chat
    # as an unseen notice.
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    with kbc.connect() as conn:
        assert [s["chat_id"] for s in kbn.list_notify_subs(conn, tid)] == [CHAT]
        assert board.unblock_task(conn, tid)
    assert _open_for_chat(board)[0] == 1
    with kbc.connect() as conn:
        assert board.block_task(conn, tid, reason="renewal failed: re-sign in", kind="needs_input",
                                human_request=HUMAN_REQUEST) is not False
        assert board.get_task(conn, tid).status == "blocked"
    assert ksa._read_unseen(path).get(CHAT) == 1  # the block notice reaches the chat
    assert ksa._read_counts(path, ksa.BLOCKED_STATUSES).get(CHAT) == 1  # and the chat shows the ask


def test_watch_is_sticky_across_a_repark_without_it(board):
    tid = _chat_card(board)
    _schedule(tid, 72, ref="first expiry", watch=True)
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        assert board.unblock_task(conn, tid)
    out = _schedule(tid, 48, ref="second expiry")
    assert out.get("watch") is True, out
    assert _open_for_chat(board)[0] == 0
    with kbc.connect() as conn:
        assert board.unblock_task(conn, tid)
    out = _schedule(tid, 48, ref="third expiry", watch=False)
    assert out["status"] == "scheduled" and not out.get("watch"), out
    assert _open_for_chat(board)[0] == 1


def test_long_park_without_watch_names_the_chat_it_holds(board):
    tid = _chat_card(board)
    out = _schedule(tid, 72)
    assert out["status"] == "scheduled", out
    hint = out.get("chat_hold") or ""
    assert f"This keeps chat {CHAT} unfinished until" in hint, out
    assert "watch=true" in hint
    assert _open_for_chat(board)[0] == 1  # without watch the chat is still held (today's behaviour)


def test_short_park_has_no_hint(board):
    tid = _chat_card(board)
    out = _schedule(tid, 2)
    assert "chat_hold" not in out, out


def test_complete_on_scheduled_card_says_not_running(board):
    tid = _chat_card(board)
    _schedule(tid, 72)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "verified later"}))
    assert out.get("error"), out
    assert out["rejection"] == "not_running"
    assert out["rejection"] in kt.COMPLETE_REJECTION_CLASSES
    assert "card is scheduled with no run" in out["error"]
    assert "archive it with a comment" in out["error"]
    assert "watch=true" in out["error"]
