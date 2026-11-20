"""A typed send refused because another window's backend holds the chat for a turn Hermes started
itself (Kanban notice, finished process, post-update continuation) is flagged ``auto_take_over``: the
Desktop then takes the chat over and re-sends instead of showing the "open somewhere else" card.
A chat whose newest turn someone typed keeps the card. [daveey]
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest

from hermes_cli.active_sessions import SESSION_NOT_OWNED, ActiveSessionRefusal
from tui_gateway import server
from tui_gateway.session_lifecycle import AUTOMATED_TURN_KINDS, latest_user_turn_kind

KEY = "20261120_162328_09e2eb"


def _conn(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                 "display_kind TEXT, active INTEGER, content TEXT)")
    conn.executemany("INSERT INTO messages (session_id, role, display_kind, active) VALUES (?,?,?,?)", rows)
    return conn


def test_latest_user_turn_kind_reads_the_newest_live_user_row():
    conn = _conn([
        (KEY, "user", None, 1),
        (KEY, "assistant", None, 1),
        (KEY, "user", "kanban_notice", 1),
        (KEY, "assistant", None, 1),
        (KEY, "user", None, 0),  # rewound away: not live
        ("other", "user", None, 1),  # another chat
    ])
    assert latest_user_turn_kind(conn, KEY) == "kanban_notice"


def test_latest_user_turn_kind_typed_row_and_empty_chat():
    assert latest_user_turn_kind(_conn([(KEY, "user", "kanban_notice", 1), (KEY, "user", None, 1)]), KEY) is None
    assert latest_user_turn_kind(_conn([]), KEY) is None


class _DB:
    def __init__(self, conn):
        self._conn = conn

    @contextlib.contextmanager
    def _read_ctx(self):
        yield self._conn


def _wire_db(monkeypatch, conn):
    @contextlib.contextmanager
    def session_db(session):
        yield _DB(conn)

    monkeypatch.setattr(server, "_session_db", session_db)


@pytest.mark.parametrize("kind", sorted(AUTOMATED_TURN_KINDS))
def test_automation_holder_kind_names_automated_turns(monkeypatch, kind):
    _wire_db(monkeypatch, _conn([(KEY, "user", None, 1), (KEY, "user", kind, 1)]))
    assert server._automation_holder_kind({"session_key": KEY}) == kind


@pytest.mark.parametrize("kind", [None, "hidden"])
def test_automation_holder_kind_ignores_typed_and_hidden_turns(monkeypatch, kind):
    _wire_db(monkeypatch, _conn([(KEY, "user", "kanban_notice", 1), (KEY, "user", kind, 1)]))
    assert server._automation_holder_kind({"session_key": KEY}) is None


def test_automation_holder_kind_fails_closed_on_store_errors(monkeypatch):
    @contextlib.contextmanager
    def broken(session):
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "_session_db", broken)
    assert server._automation_holder_kind({"session_key": KEY}) is None
    assert server._automation_holder_kind({"session_key": ""}) is None


def _refusal(reason=SESSION_NOT_OWNED):
    return ActiveSessionRefusal(
        "This chat is active in another Hermes window/terminal. Use Take over here (session.take_over), "
        "or wait for that turn to finish.\nDetails: session x opened by desktop. Writer: pid=1:pidns=2:"
        "turn=x:platform=desktop", reason)


def _data(resp):
    assert resp["error"]["code"] == 4090
    return resp["error"]["data"]


def test_slot_refusal_flags_a_chat_held_for_an_automated_turn(monkeypatch):
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: "kanban_notice")
    data = _data(server._slot_refusal("r", "s", {"session_key": KEY}, "hi", {}, _refusal(), False))
    assert data == {"reason": SESSION_NOT_OWNED, "auto_take_over": True, "holder_turn_kind": "kanban_notice"}


def test_slot_refusal_keeps_the_card_when_someone_typed_there(monkeypatch):
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: None)
    data = _data(server._slot_refusal("r", "s", {"session_key": KEY}, "hi", {}, _refusal(), False))
    assert data == {"reason": SESSION_NOT_OWNED}


def test_slot_refusal_never_flags_hidden_hosted_or_other_refusals(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: seen.append(1) or "kanban_notice")
    session = {"session_key": KEY}
    hidden = _data(server._slot_refusal("r", "s", session, "hi", {"display_kind": "hidden"}, _refusal(), False))
    hosted = _data(server._slot_refusal("r", "s", session, "hi", {}, _refusal(), True))
    capped = _data(server._slot_refusal("r", "s", session, "hi", {}, _refusal("MAX_CONCURRENT_SESSIONS"), False))
    assert "auto_take_over" not in hidden and "auto_take_over" not in hosted and "auto_take_over" not in capped
    assert seen == []  # no store read for a send that can never be flagged


def test_untyped_continuation_notes_count_as_automated():
    """Older auto-continue notes and pause replays were stored untyped; history sniffs their prefix."""
    from tui_gateway.session_history import _AUTO_CONTINUE_NOTE_PREFIX
    from tui_gateway.update_pause import PAUSE_REPLAY_NOTE_PREFIX
    for note in (_AUTO_CONTINUE_NOTE_PREFIX + " by a Hermes update.", PAUSE_REPLAY_NOTE_PREFIX + " carry on"):
        conn = _conn([])
        conn.execute("INSERT INTO messages (session_id, role, display_kind, active, content) VALUES (?,?,?,?,?)",
                     (KEY, "user", None, 1, note))
        assert latest_user_turn_kind(conn, KEY) == "auto_continue"


def test_loop_ticks_are_the_users_own_work():
    assert "loop_tick" not in AUTOMATED_TURN_KINDS
    assert {"internal_notification", "kanban_notice", "process_complete", "auto_continue"} <= AUTOMATED_TURN_KINDS


def _submit_refused(monkeypatch, holder_kind, params=None):
    """A real ``prompt.submit`` request for a live runtime whose slot claim is refused SESSION_NOT_OWNED."""
    import threading
    sid = "rt-auto-takeover"
    monkeypatch.setitem(server._sessions, sid, {
        "session_key": KEY, "history": [], "history_lock": threading.RLock(), "running": False,
        "attached_images": [], "profile_home": None, "transport": None})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: _refusal())
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: holder_kind)
    return server.handle_request({"id": "r1", "method": "prompt.submit",
                                  "params": {"session_id": sid, "text": "typed here", **(params or {})}})


def test_prompt_submit_flags_a_refusal_held_for_an_automated_turn(monkeypatch):
    resp = _submit_refused(monkeypatch, "process_complete")
    assert resp["error"]["code"] == 4090
    assert resp["error"]["data"] == {"reason": SESSION_NOT_OWNED, "auto_take_over": True,
                                     "holder_turn_kind": "process_complete"}


def test_prompt_submit_keeps_the_plain_refusal_for_a_typed_holder(monkeypatch):
    resp = _submit_refused(monkeypatch, None)
    assert resp["error"]["code"] == 4090 and resp["error"]["data"] == {"reason": SESSION_NOT_OWNED}


def test_turn_claim_refusal_is_flagged_the_same_way(monkeypatch):
    """``_claim_submit_turn`` (the idle reaper released the lease after the precheck) answers through
    the same ``_slot_refusal``."""
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: "kanban_notice")
    monkeypatch.setattr(server, "_lock_in_submit_turn", lambda *a, **k: (_refusal(), {}))
    resp, fields = server._claim_submit_turn("r", "s", {"session_key": KEY}, "hi", {}, False, None, None, None, False)
    assert fields == {} and resp["error"]["data"]["auto_take_over"] is True


def test_automation_holder_kind_reads_a_real_session_db(monkeypatch, tmp_path):
    """The store read against the real schema: a notice row after a typed row flags; a typed row after it does not."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(KEY, source="desktop")
    db.append_message(KEY, "user", "please check the card")
    db.append_message(KEY, "assistant", "on it")
    db.append_message(KEY, "user", "✔ Kanban t_1 done", display_kind="kanban_notice")

    @contextlib.contextmanager
    def session_db(session):
        yield db

    monkeypatch.setattr(server, "_session_db", session_db)
    assert server._automation_holder_kind({"session_key": KEY}) == "kanban_notice"
    db.append_message(KEY, "user", "typed in the other window")
    assert server._automation_holder_kind({"session_key": KEY}) is None
