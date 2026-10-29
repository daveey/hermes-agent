"""RPC-level tests for the generic hidden-session surface (tui_gateway).

Covers the two seams Bot Mode's "sessions are always hidden" policy leans on:

* ``session.set_hidden`` resolves a DURABLE stored session id when no live
  runtime session matches — plugins reconciling sessions they own (Bot Mode's
  hide sweep) hold stored ids for chats that aren't live right now. The old
  live-only lookup failed those with 4001 and the sweep silently no-opped.
* ``session.list`` honors ``include_hidden`` so owning surfaces (the Bots
  pane's per-profile browser) can still enumerate the rows they hid, while
  every default caller keeps the hidden rows dropped.
"""

import types

import pytest

import tui_gateway.server as srv
import tui_gateway.methods_session
from hermes_state import SessionDB
from tui_gateway.transport import AGENT_CLIENT_KIND, bind_transport, reset_transport


@pytest.fixture
def db(tmp_path, monkeypatch):
    database = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: database)
    try:
        yield database
    finally:
        database.close()


def _call(method: str, params: dict) -> dict:
    return srv._methods[method](1, params)


def _seed(db, sid: str) -> None:
    db.create_session(sid, source="desktop")
    db._conn.execute("UPDATE sessions SET message_count = 1 WHERE id = ?", (sid,))
    db._conn.commit()


def test_set_hidden_resolves_stored_id_without_live_session(db):
    """A stored (non-live) session id must be hideable — the sweep path."""
    _seed(db, "stored-chat")
    assert srv._find_live_session_by_key("stored-chat") is None

    envelope = _call("session.set_hidden", {"session_id": "stored-chat", "hidden": True})
    assert "error" not in envelope, envelope
    assert envelope["result"]["hidden"] is True
    assert db.get_session("stored-chat")["hidden"] == 1

    # And back — unhide through the same durable path.
    envelope = _call("session.set_hidden", {"session_id": "stored-chat", "hidden": False})
    assert "error" not in envelope, envelope
    assert db.get_session("stored-chat")["hidden"] == 0


def test_set_hidden_unknown_id_still_errors(db):
    envelope = _call("session.set_hidden", {"session_id": "no-such-session", "hidden": True})
    assert envelope.get("error"), envelope


def test_set_hidden_without_flag_leaves_visible_session_alone(db):
    """#122190: a flag-less ``session.set_hidden`` is rejected, never treated as hide."""
    _seed(db, "plain-chat")
    assert db.get_session("plain-chat")["hidden"] == 0

    envelope = _call("session.set_hidden", {"session_id": "plain-chat"})
    assert envelope.get("error"), envelope
    assert db.get_session("plain-chat")["hidden"] == 0


def test_session_list_include_hidden(db):
    _seed(db, "plain-chat")
    _seed(db, "bot-chat")
    assert db.set_session_hidden("bot-chat", True) is True

    default_rows = _call("session.list", {})["result"]["sessions"]
    assert {s["id"] for s in default_rows} == {"plain-chat"}

    all_rows = _call("session.list", {"include_hidden": True})["result"]["sessions"]
    assert {s["id"] for s in all_rows} == {"plain-chat", "bot-chat"}


@pytest.mark.parametrize("source", ["oneshot", "kanban", "tool"])
def test_session_list_hides_internal_sources(db, source):
    """Finite one-shot runs (`hermes -z`, `chat -q`) and other non-conversation rows never reach the
    human picker; interactive rows stay (#112550)."""
    _seed(db, "plain-chat")
    db.create_session("internal-run", source=source)
    db._conn.execute("UPDATE sessions SET message_count = 1 WHERE id = ?", ("internal-run",))
    db._conn.commit()

    rows = _call("session.list", {})["result"]["sessions"]
    assert {s["id"] for s in rows} == {"plain-chat"}


# ── agent-declared transports (/api/ws?client=agent): chats are born hidden [daveey] ───────────────────────


def _transport(client_kind=None):
    return types.SimpleNamespace(client_kind=client_kind, auth_identity=None, _peer="test", write=lambda obj: True)


@pytest.fixture
def create_env(db, monkeypatch):
    monkeypatch.setattr(srv, "_schedule_agent_build", lambda sid: None)
    monkeypatch.setattr(srv, "_schedule_session_cap_enforcement", lambda: None)
    created: list = []
    yield created
    for sid in created:
        srv._sessions.pop(sid, None)


def _create_on(create_env, db, client_kind, params):
    """``session.create`` over a transport of *client_kind*; returns (sid, session, persisted row)."""
    token = bind_transport(_transport(client_kind))
    try:
        envelope = _call("session.create", params)
    finally:
        reset_transport(token)
    assert "error" not in envelope, envelope
    sid = envelope["result"]["session_id"]
    create_env.append(sid)
    session = srv._sessions[sid]
    assert srv._ensure_session_db_row(session)
    return sid, session, db.get_session(session["session_key"])


def test_agent_transport_create_without_hidden_is_born_hidden(create_env, db):
    _sid, session, row = _create_on(create_env, db, AGENT_CLIENT_KIND, {"title": "probe"})
    assert row["hidden"] == 1
    assert {s["id"] for s in _call("session.list", {})["result"]["sessions"]}.isdisjoint({session["session_key"]})


def test_agent_transport_explicit_hidden_false_stays_visible(create_env, db):
    _sid, _session, row = _create_on(create_env, db, AGENT_CLIENT_KIND, {"hidden": False})
    assert row["hidden"] == 0


@pytest.mark.parametrize("client_kind", [None, "renderer"])
def test_renderer_transport_create_stays_visible(create_env, db, client_kind):
    """No marker (the renderer, today) or any other declared kind: unchanged, visible."""
    _sid, _session, row = _create_on(create_env, db, client_kind, {})
    assert row["hidden"] == 0


def test_renderer_transport_explicit_hidden_true_still_hides(create_env, db):
    _sid, _session, row = _create_on(create_env, db, None, {"hidden": True})
    assert row["hidden"] == 1


def test_hidden_agent_chat_still_auto_continues_and_gets_kanban_notices(create_env, db, monkeypatch, tmp_path):
    """Born hidden changes the sidebar only: the chat still auto-continues and receives kanban notices."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn
    from tui_gateway.turn_marker import record_turn_start

    sid, session, row = _create_on(create_env, db, AGENT_CLIENT_KIND, {})
    assert row["hidden"] == 1
    key = session["session_key"]

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="agent probe card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=key)
        kb.complete_task(conn, tid, summary="probe finished")
    finally:
        conn.close()
    notices = srv._collect_kanban_notifications(session)
    assert len(notices) == 1 and tid in notices[0] and "probe finished" in notices[0]

    class _InlineThread:
        def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            if self._target is not None:
                self._target(*self._args, **self._kwargs)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(srv, "_hermes_home", tmp_path)
    monkeypatch.setattr(srv.threading, "Thread", _InlineThread)
    monkeypatch.setattr(srv, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(srv, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(srv, "_load_cfg", dict)
    monkeypatch.setattr(srv, "_emit", lambda *a, **k: None)
    submitted: list = []
    monkeypatch.setattr(srv, "_run_prompt_submit", lambda rid, sid, session, text, **kw: submitted.append((text, kw)))
    session["agent"] = types.SimpleNamespace()
    record_turn_start(tmp_path, key, "run the probe")
    assert srv._maybe_schedule_auto_continue(sid, session, key) is not None
    (text, kwargs), = submitted
    assert "run the probe" in text and kwargs["display_kind"] == "auto_continue"
