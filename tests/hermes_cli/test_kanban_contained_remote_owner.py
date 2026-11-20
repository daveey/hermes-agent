"""Contained-strand escalation must not replace remote owning chats. [daveey]"""
import json
import sqlite3

import pytest

from tests.hermes_cli import test_kanban_contained_strand as h

@pytest.fixture
def board(request):
    conn = request.getfixturevalue("base_board")
    conn.execute("CREATE TABLE task_chat_links(uid TEXT,task_id TEXT,chat_gateway TEXT,chat_profile TEXT,"
                 "chat_session TEXT,kind TEXT,released INTEGER,created_at REAL)")
    return conn


base_board = h.board


@pytest.mark.parametrize("source", ["link", "subscription"])
@pytest.mark.parametrize("owner", ["chat", "released", "machine", "notice"])
def test_remote_ownership_controls_escalation(board, source, owner):
    relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    session = "cron_fixture" if owner == "machine" else "20261120_000000_abcdef"
    if source == "link" or owner == "released":
        board.execute(
            "INSERT INTO task_chat_links(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
            "VALUES (?,?, 'remote-chat-host','default',?,'created',?,?)",
            ("remote-owner", relay, session, int(owner == "released"), h.NOW - 3600))
    if source == "subscription":
        board.execute(
            "INSERT INTO kanban_notify_subs(task_id,platform,chat_id,created_at,delivery_metadata) "
            "VALUES (?,'hub',?,?,?)", (relay, f"remote-chat-host/default/{session}", h.NOW - 3600,
                                      json.dumps({"reason": "touch" if owner == "notice" else "create"})))
    # Notice-only is meaningful for subscriptions, not an explicit owning link.
    expected = 0 if owner == "chat" or (source == "link" and owner == "notice") else 1
    made = h.sweep.escalate_stranded_contained(board, now=h.NOW)
    assert len(made) == expected
    assert board.execute(f"SELECT COUNT(*) FROM tasks WHERE {h.KEY}").fetchone()[0] == expected
    assert h.sweep.escalate_stranded_contained(board, now=h.NOW + 3600) == []


def test_unreadable_remote_ownership_does_not_escalate(board, monkeypatch):
    from hermes_cli import kanban_db_notify
    _relay, authority, _ = h._contained(board)
    h._finish(board, authority)

    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("ownership unavailable")

    board.execute("INSERT INTO task_chat_links VALUES ('owner',?,'remote-chat-host','default',"
                  "'20261120_000000_abcdef','created',0,?)", (_relay, h.NOW))
    monkeypatch.setattr(kanban_db_notify, "is_machine_session", unavailable)
    assert h.sweep.escalate_stranded_contained(board, now=h.NOW) == []
    assert board.execute(f"SELECT COUNT(*) FROM tasks WHERE {h.KEY}").fetchone()[0] == 0


@pytest.mark.parametrize("reason,expected", [("shepherd", 0), ("origin", 0), ("send_refused", 1)])
def test_hub_subscription_reasons_before_link_row_lands(board, reason, expected):
    """A remote shepherd's hub sub counts as an owner even before its link row syncs; send_refused does not."""
    relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    board.execute("INSERT INTO kanban_notify_subs(task_id,platform,chat_id,created_at,delivery_metadata) "
                  "VALUES (?,'hub','remote-chat-host/default/20261120_000000_abcdef',?,?)",
                  (relay, h.NOW - 3600, json.dumps({"reason": reason})))
    assert len(h.sweep.escalate_stranded_contained(board, now=h.NOW)) == expected


@pytest.mark.parametrize("reason,expected", [("shepherd", 0), ("create", 0), (None, 0),
                                           ("touch", 1), ("send_refused", 1)])
def test_local_tui_subscription_before_link_row_lands(board, reason, expected):
    relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    board.execute("INSERT INTO kanban_notify_subs(task_id,platform,chat_id,created_at,delivery_metadata) "
                  "VALUES (?,'tui','20261120_000000_abcdef',?,?)",
                  (relay, h.NOW - 3600, json.dumps({"reason": reason}) if reason else None))
    assert len(h.sweep.escalate_stranded_contained(board, now=h.NOW)) == expected


@pytest.mark.parametrize("gateway,kind,resolved,foreign,expected", [
    ("local", "created", True, False, 0),
    ("local", "created", False, False, 1),
    ("local", "shepherd", True, False, 1),
    ("remote-chat-host", "created", True, False, 1),
    ("local", "created", True, True, 1),
])
def test_released_creator_link_matches_local_reconciliation(board, monkeypatch, gateway, kind,
                                                          resolved, foreign, expected):
    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: "local")
    relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    board.execute("INSERT INTO task_chat_links VALUES ('owner',?,?,'default',"
                  "'20261120_000000_abcdef',?,1,?)", (relay, gateway, kind, h.NOW))
    if resolved:
        h.kb._append_event(board, relay, "creator_chats_resolved", {})
    if foreign:
        board.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        board.execute("UPDATE tasks SET created_on='remote-card-host' WHERE id=?", (relay,))
    # Test ownership directly too: escalation correctly skips foreign retained copies independently.
    assert (relay in h.sweep._contained_chat_owners(board, [relay])) == (expected == 0)
    if not foreign:
        assert len(h.sweep.escalate_stranded_contained(board, now=h.NOW)) == expected


@pytest.mark.parametrize("session", ["cron_fixture", "kanban:fixture", "local-test"])
def test_released_local_creator_still_filters_non_chat(board, monkeypatch, session):
    from hermes_cli import kanban_origin, kanban_db_notify, kanban_placement
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: "local")
    relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    h.kb._append_event(board, relay, "creator_chats_resolved", {})
    board.execute("INSERT INTO task_chat_links VALUES ('owner',?,'local','default',?,'created',1,?)",
                  (relay, session, h.NOW))
    # Exercise both exclusion boundaries deterministically, independent of host session inventory.
    monkeypatch.setattr(kanban_db_notify, "is_machine_session", lambda g, s, **k: session != "local-test")
    monkeypatch.setattr(kanban_placement, "is_placeholder_session", lambda s: session == "local-test")
    assert len(h.sweep.escalate_stranded_contained(board, now=h.NOW)) == 1


def test_ownership_read_is_per_card_not_board_wide(board, monkeypatch):
    from hermes_cli import kanban_chat_links
    relay, authority, _ = h._contained(board)
    h._finish(board, authority)
    board.execute("INSERT INTO task_chat_links VALUES ('owner',?,'remote-chat-host','default',"
                  "'20261120_000000_abcdef','created',0,?)", (relay, h.NOW))
    monkeypatch.setattr(kanban_chat_links, "_local_links",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("board-wide scan")))
    assert h.sweep.escalate_stranded_contained(board, now=h.NOW) == []
