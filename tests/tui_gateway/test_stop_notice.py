"""A client Stop names who sent it, and the stopped turn leaves a visible timeline notice [daveey].

A remote client (the Redde iPhone app, logged in as ``basic:redde``) can stop a running turn with
``session.interrupt``. The turn then ended with only a hidden empty assistant row, so the chat just went
quiet and read as "stuck for hours". Stop now records its issuer and the interrupted turn persists a
``turn_stopped`` notice ("Stopped by redde"), which the Desktop renders as a system line.
"""

from __future__ import annotations

import threading
import types

import pytest
pytestmark = pytest.mark.usefixtures("no_background_link_refresh")

from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport


def record(sid: str, **state) -> dict:
    session = {
        "agent": None,
        "agent_ready": threading.Event(),
        "agent_error": None,
        "history": [],
        "history_lock": threading.RLock(),
        "running": False,
        "session_key": sid,
        **state,
    }
    server._sessions[sid] = session
    return session


@pytest.fixture
def sid(request):
    sid = f"stopnote-{request.node.name}"[:60]
    yield sid
    server._sessions.pop(sid, None)


def _transport(identity=None):
    return types.SimpleNamespace(auth_identity=identity, _peer="2600:1010::1", write=lambda obj: True)


def interrupt_as(sid: str, transport) -> dict:
    token = bind_transport(transport)
    try:
        return server._methods["session.interrupt"](1, {"session_id": sid})
    finally:
        reset_transport(token)


class _Db:
    def __init__(self):
        self.rows = []

    def append_message(self, **kw):
        self.rows.append(kw)
        return len(self.rows)


@pytest.fixture
def db(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(server, "_submit_row_target_key", lambda session: session["session_key"])
    agent = types.SimpleNamespace(_session_db=fake, interrupt=lambda *a, **k: True)
    return fake, agent


def test_stop_from_a_login_names_that_login(sid, db, caplog):
    _fake, agent = db
    session = record(sid, running=True, agent=agent)
    caplog.set_level("INFO")

    response = interrupt_as(sid, _transport({"user_id": "redde", "provider": "basic"}))

    assert response.get("result", {}).get("status") == "interrupted", response
    assert session["_stop_issuer"] == "redde"
    assert any("by=redde" in r.getMessage() and sid in r.getMessage() for r in caplog.records)


def test_stop_without_a_login_is_attributed_to_desktop(sid, db):
    _fake, agent = db
    session = record(sid, running=True, agent=agent)

    interrupt_as(sid, _transport(None))

    assert session["_stop_issuer"] == "Desktop"


def test_stop_on_an_idle_session_records_no_issuer(sid, db):
    """No live turn: nothing was stopped, so no notice may be queued for a later turn."""
    _fake, agent = db
    session = record(sid, running=False, agent=agent)

    interrupt_as(sid, _transport({"user_id": "redde", "provider": "basic"}))

    assert "_stop_issuer" not in session


def test_interrupted_turn_persists_a_visible_stop_notice(sid, db):
    fake, agent = db
    session = record(sid, running=True, agent=agent, _stop_issuer="redde")

    entry = server._record_stop_notice(session, "interrupted")

    assert entry is not None
    assert session["history"][-1] is entry
    assert entry["role"] == "user" and entry["display_kind"] == "turn_stopped"
    assert entry["display_metadata"]["display_text"] == "Stopped by redde"
    assert "redde" in entry["content"]
    assert len(fake.rows) == 1
    row = fake.rows[0]
    assert row["session_id"] == sid and row["display_kind"] == "turn_stopped"
    assert row["display_metadata"] == {"display_text": "Stopped by redde", "stopped_by": "redde"}
    assert "_stop_issuer" not in session


def test_completed_turn_drops_a_stale_issuer_without_a_notice(sid, db):
    """A Stop that raced a turn which finished anyway must not leave a notice behind."""
    fake, agent = db
    session = record(sid, running=True, agent=agent, _stop_issuer="redde")

    assert server._record_stop_notice(session, "complete") is None

    assert session["history"] == [] and fake.rows == []
    assert "_stop_issuer" not in session


def test_system_interrupt_without_a_client_stop_adds_no_notice(sid, db):
    """Reapers, lease loss and shutdown have their own reporting: only a client Stop adds the notice."""
    fake, agent = db
    session = record(sid, running=True, agent=agent)

    assert server._record_stop_notice(session, "interrupted") is None
    assert fake.rows == []


# ── end to end: a real prompt.submit turn stopped mid-run by a logged-in client ──────────────────────

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


@pytest.fixture()
def emitted(monkeypatch, tmp_path):
    frames = []
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None, *a, **k: frames.append((event, payload)))
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_submit_row_target_key", lambda session: session["session_key"])
    return frames


def _run_turn(sid, identity, *, stop: bool):
    fake = _Db()

    def run_conversation(*a, **k):
        if stop:  # the client's Stop lands while the turn is running
            interrupt_as(sid, _transport(identity))
        return {"final_response": "", "messages": [], "interrupted": stop, "completed": not stop}

    agent = types.SimpleNamespace(session_id="agent-sid-1", run_conversation=run_conversation,
                                  clear_interrupt=lambda: None, interrupt=lambda *a, **k: True, _session_db=fake)
    session = record(sid, running=True, agent=agent, history_version=0, attached_images=[], image_counter=0,
                     cols=80, slash_worker=None, show_reasoning=False, tool_progress_mode="all", inflight_turn=None)
    server._run_prompt_submit("rid", sid, session, "go")
    return session, fake


def test_turn_stopped_by_redde_ends_with_a_persisted_notice(sid, emitted):
    session, fake = _run_turn(sid, {"user_id": "redde", "provider": "basic"}, stop=True)

    payload = [p for event, p in emitted if event == "message.complete"][-1]
    assert payload["status"] == "interrupted"
    notices = [h for h in session["history"] if h.get("display_kind") == "turn_stopped"]
    assert [n["display_metadata"]["display_text"] for n in notices] == ["Stopped by redde"]
    assert [r["display_kind"] for r in fake.rows] == ["turn_stopped"]
    assert "_stop_issuer" not in session


def test_turn_that_completes_adds_no_notice(sid, emitted):
    session, fake = _run_turn(sid, None, stop=False)

    assert not [h for h in session["history"] if h.get("display_kind") == "turn_stopped"]
    assert fake.rows == []


# ── SSH-attached Desktop identity + the stop gesture (origin) ─────────────────────────────────────
# A Desktop attached over SSH logs in with a per-connection ownership id (provider ``desktop-ssh``), so
# the notice read "Stopped by 5caca28140205b2b8f1f5cabb2aaf8ba". And every Stop gesture sent the same
# RPC, so "why did this stop?" had no answer in agent.log.

SSH_DESKTOP = {"user_id": "5caca28140205b2b8f1f5cabb2aaf8ba", "provider": "desktop-ssh"}


def interrupt_with(sid: str, transport, **extra) -> dict:
    token = bind_transport(transport)
    try:
        return server._methods["session.interrupt"](1, {"session_id": sid, **extra})
    finally:
        reset_transport(token)


def test_stop_from_an_ssh_attached_desktop_reads_desktop(sid, db, caplog):
    fake, agent = db
    session = record(sid, running=True, agent=agent)
    caplog.set_level("INFO")

    interrupt_as(sid, _transport(SSH_DESKTOP))
    entry = server._record_stop_notice(session, "interrupted")

    assert entry["display_metadata"]["display_text"] == "Stopped by Desktop"
    assert fake.rows[0]["display_metadata"]["display_text"] == "Stopped by Desktop"
    assert SSH_DESKTOP["user_id"] not in entry["content"]
    assert any("by=Desktop" in r.getMessage() and sid in r.getMessage() for r in caplog.records)


def test_ssh_desktop_turn_end_to_end_reads_desktop(sid, emitted):
    session, _fake = _run_turn(sid, SSH_DESKTOP, stop=True)

    notices = [h for h in session["history"] if h.get("display_kind") == "turn_stopped"]
    assert [n["display_metadata"]["display_text"] for n in notices] == ["Stopped by Desktop"]


@pytest.mark.parametrize("origin", ["stop_button", "esc", "tile", "slash", "delete", "plugin:hermes-bots"])
def test_each_stop_origin_is_logged_and_kept_in_the_notice(sid, db, caplog, origin):
    fake, agent = db
    session = record(sid, running=True, agent=agent)
    caplog.set_level("INFO")

    response = interrupt_with(sid, _transport(SSH_DESKTOP), origin=origin)
    entry = server._record_stop_notice(session, "interrupted")

    assert response.get("result", {}).get("status") == "interrupted", response
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("session.interrupt: ")]
    assert any(sid in line and "by=Desktop" in line and line.endswith(f"origin={origin}") for line in lines), lines
    assert entry["display_metadata"] == {"display_text": "Stopped by Desktop", "stopped_by": "Desktop",
                                         "stop_origin": origin}
    assert fake.rows[0]["display_metadata"]["stop_origin"] == origin
    assert "_stop_origin" not in session


def test_absent_origin_logs_a_dash_and_adds_no_metadata_key(sid, db, caplog):
    _fake, agent = db
    session = record(sid, running=True, agent=agent)
    caplog.set_level("INFO")

    interrupt_as(sid, _transport({"user_id": "redde", "provider": "basic"}))
    entry = server._record_stop_notice(session, "interrupted")

    assert any(sid in r.getMessage() and r.getMessage().endswith("origin=-") for r in caplog.records)
    assert "stop_origin" not in entry["display_metadata"]


@pytest.mark.parametrize("raw, kept", [("some new gesture!\n", "some_new_gesture_"), (42, None), ("", None),
                                       ("x" * 200, "x" * 64)])
def test_unknown_or_odd_origin_is_accepted_and_made_log_safe(sid, db, caplog, raw, kept):
    _fake, agent = db
    session = record(sid, running=True, agent=agent)
    caplog.set_level("INFO")

    response = interrupt_with(sid, _transport(None), origin=raw)
    entry = server._record_stop_notice(session, "interrupted")

    assert response.get("result", {}).get("status") == "interrupted", response
    assert entry["display_metadata"].get("stop_origin") == kept
    assert any(r.getMessage().endswith(f"origin={kept or '-'}") for r in caplog.records)


def test_origin_of_an_earlier_stop_never_labels_a_later_one(sid, db):
    """A later turn's Stop without an origin must not inherit the earlier turn's gesture."""
    _fake, agent = db
    session = record(sid, running=True, agent=agent)

    interrupt_with(sid, _transport(None), origin="esc")
    first = server._record_stop_notice(session, "interrupted")
    session["running"] = True  # the next turn
    interrupt_as(sid, _transport(None))
    second = server._record_stop_notice(session, "interrupted")

    assert first["display_metadata"]["stop_origin"] == "esc"
    assert "stop_origin" not in second["display_metadata"]


def test_a_repeat_stop_on_an_already_stopped_turn_keeps_the_gesture_that_stopped_it(sid, db):
    """Interrupt clears ``running``; a second Stop on the same turn changes neither who nor how."""
    _fake, agent = db
    session = record(sid, running=True, agent=agent)

    interrupt_with(sid, _transport(SSH_DESKTOP), origin="esc")
    interrupt_with(sid, _transport({"user_id": "redde", "provider": "basic"}), origin="stop_button")
    entry = server._record_stop_notice(session, "interrupted")

    assert entry["display_metadata"] == {"display_text": "Stopped by Desktop", "stopped_by": "Desktop",
                                         "stop_origin": "esc"}


def test_contract_accepts_origin_on_session_interrupt():
    """The wire contract refuses unknown keys (4000); ``origin`` must be a declared one."""
    from tui_gateway.contracts.registry import METHODS, validate_params

    params, problem = validate_params(METHODS["session.interrupt"], {"session_id": "s", "origin": "esc"})
    assert problem is None and params is not None
