"""[daveey] ``sessions.moved``: the change watcher names the sessions that got new message rows.

The Desktop's transcript cache fetches only the rows after its cached tail for each named session, instead of
polling the hub listing and refetching whole pages. Exercised against a real temp state.db written through
SessionDB: only moved sessions are named, a burst inside the 1 s floor is one trailing frame that loses no rows,
an idle store (or a session-row-only write) sends nothing, and the payload matches the gateway contract.
"""

import sqlite3

import pytest

from tui_gateway import server


@pytest.fixture()
def watcher(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    monkeypatch.setattr(server, "SESSION_DIGEST_MIN_S", 0.0)
    (tmp_path / "config.yaml").write_text("display: {}\n")
    (tmp_path / "cron").mkdir()
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_change_sigs", {})
    monkeypatch.setattr(server, "_change_checked_at", {})
    monkeypatch.setattr(server, "_change_broadcast_at", {})
    monkeypatch.setattr(server, "_bot_relay_outbox_seen", 0)
    monkeypatch.setattr(server, "_pairing_roots_cache", None, raising=False)
    monkeypatch.setattr(server, "_sessions_db_sig_cache", {})
    monkeypatch.setattr(server, "_sessions_moved_cursor_cache", {})

    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="busy", source="desktop")
    db.create_session(session_id="quiet", source="desktop")
    db.append_messages_batch("busy", [{"role": "user", "content": "hi"}])
    db.append_messages_batch("quiet", [{"role": "user", "content": "hello"}])

    events = []
    monkeypatch.setattr(server, "_broadcast_global_event", lambda ev, payload=None: events.append((ev, payload)))
    yield tmp_path, db, events
    db.close()


def _moved(events):
    return [payload for event, payload in events if event == "sessions.moved"]


def _max_id(home, session_id):
    with sqlite3.connect(home / "state.db") as conn:
        return conn.execute("SELECT MAX(id) FROM messages WHERE session_id = ?", (session_id,)).fetchone()[0]


def test_only_sessions_with_new_rows_are_named(watcher):
    home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)  # seeds every cursor silently
    assert _moved(events) == []

    db.append_messages_batch("busy", [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}])
    server._broadcast_watched_changes(now=10.0)

    [payload] = _moved(events)
    [entry] = payload["sessions"]
    assert entry["id"] == "busy"
    assert entry["last_message_id"] == _max_id(home, "busy")
    assert entry["message_count"] == 3
    assert entry["profile"] == "default"
    assert entry["activity"] is not None
    assert entry["parent_session_id"] is None


def test_a_burst_inside_the_floor_is_one_trailing_frame(watcher):
    home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)

    db.append_messages_batch("busy", [{"role": "assistant", "content": "1"}])
    server._broadcast_watched_changes(now=10.0)
    assert [p["sessions"][0]["id"] for p in _moved(events)] == ["busy"]

    # Two more writes 0.5 s apart: both inside the 1 s floor of the first frame.
    db.append_messages_batch("quiet", [{"role": "assistant", "content": "2"}])
    server._broadcast_watched_changes(now=10.5)
    db.append_messages_batch("busy", [{"role": "assistant", "content": "3"}])
    server._broadcast_watched_changes(now=10.75)
    assert len(_moved(events)) == 1

    # The window opens: one frame carries every row the floored passes saw, newest mover first.
    server._broadcast_watched_changes(now=11.0)
    frames = _moved(events)
    assert len(frames) == 2
    assert [(e["id"], e["last_message_id"]) for e in frames[1]["sessions"]] == [
        ("busy", _max_id(home, "busy")), ("quiet", _max_id(home, "quiet"))]


def test_an_idle_store_sends_nothing(watcher):
    _home, _db, events = watcher
    for tick in range(20):
        server._broadcast_watched_changes(now=float(tick))
    assert _moved(events) == []


def test_a_session_row_write_without_messages_sends_no_moved_frame(watcher):
    _home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)

    db.set_session_title("quiet", "renamed")
    server._broadcast_watched_changes(now=10.0)

    assert ("sessions.changed", {}) in events  # the list still refreshes
    assert _moved(events) == []


def test_a_new_child_names_its_parent(watcher):
    _home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)

    db.create_session(session_id="busy-child", source="desktop", parent_session_id="busy")
    db.append_messages_batch("busy-child", [{"role": "user", "content": "continued"}])
    server._broadcast_watched_changes(now=10.0)

    [payload] = _moved(events)
    assert [(e["id"], e["parent_session_id"]) for e in payload["sessions"]] == [("busy-child", "busy")]


def test_payload_matches_the_gateway_contract(watcher):
    from tui_gateway.contracts.events import SessionsMovedPayload
    from tui_gateway.contracts.registry import EVENTS

    _home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)
    db.append_messages_batch("busy", [{"role": "assistant", "content": "x"}])
    server._broadcast_watched_changes(now=10.0)

    [payload] = _moved(events)
    assert SessionsMovedPayload.model_validate(payload).sessions[0].id == "busy"
    assert "sessions.moved" in EVENTS


def test_a_non_sqlite_store_neither_raises_nor_sends(watcher, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / "state.db").write_text("x")
    monkeypatch.setattr(server, "_hermes_home", str(other))
    events = watcher[2]
    server._broadcast_watched_changes(now=0.0)
    (other / "state.db").write_text("xy")
    server._broadcast_watched_changes(now=10.0)
    assert _moved(events) == []


# ── review of #3912 ──────────────────────────────────────────────────────────────────────────────


def test_a_failed_row_read_keeps_the_range_for_the_next_pass(watcher, monkeypatch):
    home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)
    db.append_messages_batch("busy", [{"role": "assistant", "content": "lost?"}])

    real = server._moved_sessions
    monkeypatch.setattr(server, "_moved_sessions", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("busy")))
    server._broadcast_watched_changes(now=10.0)
    assert _moved(events) == []  # nothing sent, and no floor started

    monkeypatch.setattr(server, "_moved_sessions", real)
    server._broadcast_watched_changes(now=10.5)
    [payload] = _moved(events)
    assert [(e["id"], e["last_message_id"]) for e in payload["sessions"]] == [("busy", _max_id(home, "busy"))]


def test_an_unreadable_cursor_keeps_the_range_for_the_next_pass(watcher, monkeypatch):
    _home, db, events = watcher
    server._broadcast_watched_changes(now=0.0)
    db.append_messages_batch("quiet", [{"role": "assistant", "content": "x"}])

    real = server._sessions_moved_sig
    monkeypatch.setattr(server, "_sessions_moved_sig", lambda: tuple((h, None) for h, _c in real()))
    monkeypatch.setitem(server._CHANGE_WATCHES, "sessions.moved",
                        (0.5, server._sessions_moved_sig, dict, server._sessions_moved_payload))
    server._broadcast_watched_changes(now=10.0)
    assert _moved(events) == []

    monkeypatch.setitem(server._CHANGE_WATCHES, "sessions.moved",
                        (0.5, real, dict, server._sessions_moved_payload))
    server._broadcast_watched_changes(now=10.5)
    [payload] = _moved(events)
    assert [e["id"] for e in payload["sessions"]] == ["quiet"]


@pytest.mark.parametrize("make", [
    lambda db: db.create_session(session_id="t", source="tool"),
    lambda db: db.create_session(session_id="t", source="subagent"),
    lambda db: db.create_session(session_id="t", source="desktop", model_config={"_delegate_from": "busy"}),
    lambda db: (db.create_session(session_id="t", source="desktop"),
                sqlite3.connect(db.db_path).execute("UPDATE sessions SET hidden = 1 WHERE id = 't'").connection.commit()),
], ids=["tool", "subagent", "delegate-child", "hidden"])
def test_sessions_the_listing_never_shows_are_not_pushed(watcher, make):
    _home, db, events = watcher
    make(db)
    server._broadcast_watched_changes(now=0.0)

    db.append_messages_batch("t", [{"role": "assistant", "content": "internal"}])
    db.append_messages_batch("busy", [{"role": "assistant", "content": "visible"}])
    server._broadcast_watched_changes(now=10.0)

    [payload] = _moved(events)
    assert [e["id"] for e in payload["sessions"]] == ["busy"]
