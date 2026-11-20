"""A card block/unblock/complete/archive refreshes the origin chat's stored outcome after commit.

[daveey] The stored ``sessions.outcome_json`` used to refresh only on chat triggers and the card-change poll, so the
stripe kept painting a withdrawn ask until the next tick. ``refreshes_origin_outcomes`` wraps the four transitions.
"""

from __future__ import annotations

import threading
import types

import pytest

import hermes_state_outcome as outcome
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_outcome_refresh as refresh
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli import test_kanban_session_outcome_tasks as helpers

kanban_root = helpers.kanban_root

REQUEST = helpers.HUMAN_REQUEST


@pytest.fixture
def board(kanban_root, monkeypatch):
    monkeypatch.setattr(refresh, "owns_chat_sessions", lambda: True)  # pretend: the TUI gateway process
    conn = connect(kanban_root / "kanban.db")
    yield conn
    conn.close()


@pytest.fixture
def seen(monkeypatch):
    calls: list[list[str]] = []
    done = threading.Event()

    def fake(chats):
        calls.append(list(chats))
        done.set()
        return len(chats)

    monkeypatch.setattr(refresh, "refresh_chats", fake)
    return types.SimpleNamespace(calls=calls, done=done)


def _wait(seen):
    assert seen.done.wait(5), "no post-commit outcome refresh"
    seen.done.clear()


def test_block_unblock_archive_each_refresh_the_origin_chat(board, seen):
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat-1")
    assert kb.block_task(board, task, kind="needs_input", human_request=REQUEST)
    _wait(seen)
    assert kb.unblock_task(board, task)
    _wait(seen)
    assert kb.archive_task(board, task)
    _wait(seen)
    assert seen.calls == [["chat-1"]] * 3


def test_complete_refreshes_the_origin_chat(board, seen):
    task = kb.create_task(board, title="Do", assignee="worker", session_id="chat-1")
    assert kb.complete_task(board, task, summary="done")
    _wait(seen)
    assert seen.calls == [["chat-1"]]


def test_a_refused_transition_refreshes_nothing(board, seen):
    task = kb.create_task(board, title="Do", assignee="worker", session_id="chat-1")
    assert not kb.unblock_task(board, task)  # ready, not blocked
    assert not seen.done.wait(0.3)


def test_shepherd_subscribers_refresh_but_touch_subscribers_do_not(board, seen):
    task = kb.create_task(board, title="Do", assignee="worker", session_id="origin")
    board.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, delivery_metadata, created_at) "
                  "VALUES (?, 'tui', 'shepherd', '{\"reason\": \"shepherd\"}', 1)", (task,))
    board.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, delivery_metadata, created_at) "
                  "VALUES (?, 'tui', 'toucher', '{\"reason\": \"touch\"}', 1)", (task,))
    board.commit()
    assert kb.block_task(board, task, kind="needs_input", human_request=REQUEST)
    _wait(seen)
    assert seen.calls == [["origin", "shepherd"]]


def _link(board, task, chat, released):
    board.execute("CREATE TABLE IF NOT EXISTS task_chat_links (id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT NOT NULL, "
                  "chat_gateway TEXT NOT NULL, chat_profile TEXT, chat_session TEXT NOT NULL, kind TEXT NOT NULL, "
                  "released INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL)")
    board.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_session, kind, released, created_at) "
                  "VALUES (?, ?, 'gw', ?, 'created', ?, 1)", (f"{chat}-{released}", task, chat, released))
    board.commit()


def test_a_chat_whose_every_link_is_released_is_not_refreshed(board):
    task = kb.create_task(board, title="Do", assignee="worker", session_id="origin")
    for chat in ("gone", "kept"):
        board.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, delivery_metadata, created_at) "
                      "VALUES (?, 'tui', ?, '{\"reason\": \"shepherd\"}', 1)", (task, chat))
    _link(board, task, "gone", 1)
    _link(board, task, "kept", 1)
    _link(board, task, "kept", 0)  # one live link keeps the chat counted
    assert refresh.linked_chats(board, task) == ["origin", "kept"]


def test_no_refresh_inside_a_callers_open_transaction(board, seen):
    task = kb.create_task(board, title="Do", assignee="worker", session_id="chat-1")
    board.execute("BEGIN")
    refresh.after_transition(board, task)
    board.execute("ROLLBACK")
    assert not seen.done.wait(0.3)


def test_a_process_without_chat_sessions_leaves_it_to_the_poller(board, seen, monkeypatch):
    monkeypatch.setattr(refresh, "owns_chat_sessions", lambda: False)
    task = kb.create_task(board, title="Do", assignee="worker", session_id="chat-1")
    assert kb.block_task(board, task, kind="needs_input", human_request=REQUEST)
    assert not seen.done.wait(0.3)


def test_refresh_chats_refreshes_the_tip_with_the_card_change_trigger(monkeypatch):
    import contextlib

    from tui_gateway import kanban_unloaded as ku
    from tui_gateway import notice_fanout

    calls = []

    @contextlib.contextmanager
    def find(chat):
        yield (object(), "tip-of-" + chat)

    monkeypatch.setattr(ku, "find_session_db", find)
    monkeypatch.setattr(notice_fanout, "chat_skip_reason", lambda db, chat: "archived" if chat == "old" else None)
    monkeypatch.setattr(outcome, "refresh_outcomes_quietly",
                        lambda sids, trigger, db=None: calls.append((list(sids), trigger)) or len(list(sids)))
    assert refresh.refresh_chats(["chat-1", "old"]) == 1
    assert calls == [(["tip-of-chat-1"], "card_change")]


def test_archiving_a_blocked_card_withdraws_the_decision_from_the_outcome(kanban_root):
    """The live case: an archived card left ``outcome:decision`` / ``kanban-blocked`` behind."""
    conn = connect(kanban_root / "kanban.db")
    try:
        task = kb.create_task(conn, title="Deploy", assignee="worker", session_id="chat-1")
        assert kb.block_task(conn, task, kind="needs_input", human_request=REQUEST)
        ksa._reset_cache_for_tests()
        decisions, _ = outcome._card_items(["chat-1"])
        assert [d["card_id"] for d in decisions] == [task]
        assert kb.archive_task(conn, task)
        ksa._reset_cache_for_tests()
        decisions, waits = outcome._card_items(["chat-1"])
        assert decisions == [] and waits == []
        rows = [{"id": "chat-1"}]
        ksa.annotate_kanban_activity(rows)
        assert "needs_you" not in rows[0] and "kanban_blocked" not in rows[0]
    finally:
        conn.close()
