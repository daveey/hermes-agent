"""Trusted producer completions for unrouted inline sessions (hermes_state_producer_admission +
tui_gateway.producer_admission). [daveey]

A legacy inline chat (no gateway route, no local policy receipt) had no durable ingress for a producer completion
such as a finished user-terminal job: ``admit_automation`` needs a route, adoption needs an authenticated native
transport and refuses a live inline owner, and ``plugin_inject`` makes ordinary user input. These tests drive the
real SessionDB and the real poller/sweeper delivery functions; only the agent turn itself (``_run_prompt_submit``)
is stubbed.
"""

import contextlib
import json
import threading
from pathlib import Path

import pytest

import hermes_state_producer_admission as pa
from hermes_constants import get_default_hermes_root
from hermes_state import SessionDB
from tui_gateway import producer_admission as delivery
from tui_gateway import server

SID = "20261022_204322_4d8ce1"
PRODUCER = json.dumps(["user-terminal", "job-1", "final"], separators=(",", ":"))
TEXT = "[User-terminal completion - tool result, not user input]\njob_id: job-1\nstatus: exited\nexit_code: 0"


def _root() -> Path:
    return Path(get_default_hermes_root())


def _db(profile: str | None = None) -> SessionDB:
    home = _root() if not profile else _root() / "profiles" / profile
    home.mkdir(parents=True, exist_ok=True)
    return SessionDB(home / "state.db")


def _session(key: str = SID, running: bool = False) -> dict:
    return {"session_key": key, "history_lock": threading.RLock(), "running": running}


def _admit(producer=PRODUCER, sid=SID, text=TEXT, home=None):
    return pa.admit_producer_completion(producer=producer, profile_home=home or _root(), stored_session_id=sid,
                                        text=text)


def _notices(db: SessionDB, sid: str = SID) -> list:
    return [m for m in db.get_messages(sid) if m.get("display_kind") == pa.DISPLAY_KIND]


def _table_rows(db: SessionDB) -> list:
    with db._read_ctx() as conn:
        if not pa._table_exists(conn):
            return []
        return [dict(r) for r in conn.execute("SELECT * FROM producer_admissions")]


@pytest.fixture
def sdb(monkeypatch):
    d = _db()
    d.create_session(SID, "desktop")
    d.append_message(SID, "user", "run the build in my terminal")
    d.append_message(SID, "assistant", "Started it.")

    @contextlib.contextmanager
    def _opener(session):
        yield d

    monkeypatch.setattr(server, "_session_db", _opener)
    yield d
    d.close()


@pytest.fixture
def turns(monkeypatch):
    state = {"submitted": [], "accept": True}

    def _submit(rid, sid, session, text, **kwargs):
        state["submitted"].append({"sid": sid, "text": text, "kwargs": kwargs,
                                   "staged": session.get("_submit_user_row")})
        with session["history_lock"]:
            session["running"] = False
        return state["accept"]

    def _no_user_input(*_a, **_k):
        raise AssertionError("a producer completion must never be enqueued as user input")

    monkeypatch.setattr(server, "_run_prompt_submit", _submit)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_enqueue_prompt", _no_user_input, raising=False)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session, **kw: None)
    return state


# -- active inline idle owner ------------------------------------------------------------------------------------

def test_idle_inline_owner_gets_one_trusted_notice_turn(sdb, turns):
    receipt = _admit()
    assert receipt["outcome"] == "admitted" and receipt["status"] == "queued" and receipt["owner"] == SID
    before = len(sdb.get_messages(SID))

    assert delivery.deliver_pending("ui-1", _session()) == "started"

    rows = _notices(sdb)
    assert len(rows) == 1 and rows[0]["role"] == "user" and rows[0]["content"] == TEXT
    meta = rows[0]["display_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["producer_admission"] == PRODUCER
    # No plain user-authored row was fabricated: the only new row is the notice.
    assert len(sdb.get_messages(SID)) == before + 1
    [turn] = turns["submitted"]
    assert turn["text"] == TEXT and turn["kwargs"]["display_kind"] == pa.DISPLAY_KIND
    assert "turn_author" not in turn["kwargs"]
    assert turn["staged"]["_row_id"] == rows[0]["id"]  # the turn adopts the committed row, no second row
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "dispatched"
    # Nothing left: a later poll does nothing.
    assert delivery.deliver_pending("ui-1", _session()) == "none"
    assert len(_notices(sdb)) == 1 and len(turns["submitted"]) == 1


# -- busy owner -------------------------------------------------------------------------------------------------

def test_busy_owner_keeps_it_queued_until_idle(sdb, turns):
    _admit()
    busy = _session(running=True)
    assert delivery.deliver_pending("ui-1", busy) == "busy"
    assert _notices(sdb) == [] and turns["submitted"] == []
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "queued"

    busy["running"] = False
    assert delivery.deliver_pending("ui-1", busy) == "started"
    assert len(_notices(sdb)) == 1 and len(turns["submitted"]) == 1


def test_stopped_owner_does_not_start_an_automatic_turn(sdb, turns):
    _admit()
    stopped = _session()
    stopped["_turn_cancel_requested"] = True
    assert delivery.deliver_pending("ui-1", stopped) == "busy"
    assert _notices(sdb) == []


def test_another_live_owner_is_never_taken_over(sdb, turns, monkeypatch):
    seen = {}

    def _slot(sid, session, **kw):
        seen.update(kw)
        return "open in another Hermes window"

    monkeypatch.setattr(server, "_ensure_active_session_slot", _slot)
    _admit()
    session = _session()
    assert delivery.deliver_pending("ui-1", session) == "refused"
    assert seen == {"allow_takeover": False}
    assert _notices(sdb) == [] and turns["submitted"] == []
    assert session["running"] is False  # the claim was released


# -- cold history -----------------------------------------------------------------------------------------------

def _install_resume(monkeypatch, *, refuse=False):
    calls = []
    sessions: dict = {}

    def _resume(rid, params):
        calls.append(params)
        if refuse:
            return {"error": {"code": 4090, "message": "open in another Hermes window"}}
        live = _session(params["session_id"])
        live["agent"] = object()
        sessions["ui-cold"] = live
        return {"result": {"session_id": "ui-cold"}}

    monkeypatch.setitem(server._methods, "session.resume", _resume)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda key, profile_home=None: next(
        ((sid, s) for sid, s in sessions.items() if s["session_key"] == key), None))
    return calls, sessions


def test_cold_history_is_woken_by_the_sweeper_and_delivered_once(sdb, turns, monkeypatch):
    calls, _ = _install_resume(monkeypatch)
    _admit()
    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "started" and rec["owner"] == SID
    assert calls == [{"session_id": SID, "omit_messages": True}]
    assert len(_notices(sdb)) == 1 and len(turns["submitted"]) == 1
    assert delivery.sweep_once() == []  # restart-safe: nothing pending, nothing woken


def test_cold_history_resume_refused_stays_queued(sdb, turns, monkeypatch):
    _install_resume(monkeypatch, refuse=True)
    _admit()
    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "resume_refused"
    assert _notices(sdb) == [] and turns["submitted"] == []
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "queued"


def test_named_profile_cold_chat_resumes_in_its_own_profile(turns, monkeypatch):
    work = _db("work")
    work.create_session(SID, "desktop")
    work.append_message(SID, "user", "hi")

    @contextlib.contextmanager
    def _opener(session):
        yield work

    monkeypatch.setattr(server, "_session_db", _opener)
    calls, _ = _install_resume(monkeypatch)
    receipt = _admit(home=_root() / "profiles" / "work")
    assert receipt["status"] == "queued"
    recs = delivery.sweep_once()
    assert [r["verdict"] for r in recs] == ["started"] and recs[0]["profile"] == "work"
    assert calls == [{"session_id": SID, "omit_messages": True, "profile": "work"}]
    assert len(_notices(work)) == 1
    work.close()


# -- compression lineage ----------------------------------------------------------------------------------------

def _rotate(db: SessionDB, parent: str, child: str) -> None:
    db.create_session(child, "desktop", parent_session_id=parent)
    db.end_session(parent, "compression")


def test_compression_lineage_routes_to_live_tip(sdb, turns):
    child = SID + "_c1"
    _rotate(sdb, SID, child)
    sdb.append_message(child, "user", "continue")
    receipt = _admit(sid=child)
    assert receipt["owner"] == SID  # one identity per conversation, whichever segment the producer named
    assert _admit(sid=SID)["producer"] == PRODUCER

    stale = _session(SID)  # a runtime still keyed on the rotated-away segment writes nothing
    assert delivery.deliver_pending("ui-1", stale) == "moved"
    assert _notices(sdb, SID) == [] and _notices(sdb, child) == []

    assert delivery.deliver_pending("ui-1", _session(child)) == "started"
    assert len(_notices(sdb, child)) == 1 and _notices(sdb, SID) == []


def test_rotation_between_admission_and_delivery_uses_the_new_tip(sdb, turns):
    _admit()
    child = SID + "_c1"
    _rotate(sdb, SID, child)
    assert delivery.deliver_pending("ui-1", _session(child)) == "started"
    assert len(_notices(sdb, child)) == 1


# -- missing / wrong profile ------------------------------------------------------------------------------------

def test_missing_session_writes_nothing(sdb):
    assert _admit(sid="20990101_000000_nothere") == {"outcome": "missing", "reason": "not_found"}
    assert _table_rows(sdb) == []


def test_session_deleted_after_admission_is_reported_missing(sdb, turns, monkeypatch):
    _install_resume(monkeypatch)
    _admit()
    sdb.delete_session(SID)
    [rec] = delivery.sweep_once()
    assert rec["verdict"] == "missing" and turns["submitted"] == []
    receipt = pa.read_producer_admission(producer=PRODUCER, profile_home=_root())
    assert receipt["outcome"] == "missing" and receipt["reason"] == "session_deleted"


def test_wrong_profile_home_never_reaches_another_profiles_session(sdb, turns):
    other = _db("work")  # profile store exists but does not hold the default profile's chat
    assert _admit(home=_root() / "profiles" / "work") == {"outcome": "missing", "reason": "not_found"}
    assert _table_rows(other) == [] and _table_rows(sdb) == []
    other.close()


def test_row_owned_by_another_profile_is_refused(sdb):
    sdb.create_session("20261022_000000_foreign", "desktop", profile_name="work")
    assert _admit(sid="20261022_000000_foreign") == {"outcome": "missing", "reason": "wrong_profile"}


def test_unknown_profile_home(tmp_path):
    assert _admit(home=tmp_path / "nowhere") == {"outcome": "missing", "reason": "unknown_profile_home"}


def test_messaging_session_is_not_local(sdb):
    sdb.create_session("tg-1", "telegram")
    assert _admit(sid="tg-1") == {"outcome": "missing", "reason": "not_local"}


# -- lost receipt retry / repeated identity / restart -----------------------------------------------------------

def test_lost_receipt_retry_returns_the_same_admission(sdb, turns):
    first = _admit()
    assert _admit() == first
    assert len(_table_rows(sdb)) == 1
    with pytest.raises(ValueError, match="different payload"):
        _admit(text=TEXT + "\nchanged")
    delivery.deliver_pending("ui-1", _session())
    again = _admit()  # retry after delivery: the settled receipt, no second row or turn
    assert again["status"] == "dispatched" and again["message_uid"]
    assert delivery.deliver_pending("ui-1", _session()) == "none"
    assert len(_notices(sdb)) == 1 and len(turns["submitted"]) == 1


def test_refused_dispatch_redelivers_the_same_row_once(sdb, turns):
    _admit()
    turns["accept"] = False
    assert delivery.deliver_pending("ui-1", _session()) == "dispatch_refused"
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "committed"
    [row] = _notices(sdb)

    turns["accept"] = True
    assert delivery.deliver_pending("ui-1", _session()) == "started"  # e.g. a fresh process after a restart
    assert [r["id"] for r in _notices(sdb)] == [row["id"]]
    assert turns["submitted"][-1]["staged"]["_row_id"] == row["id"]
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "dispatched"


def test_turn_that_ran_before_a_crash_is_not_run_again(sdb, turns):
    _admit()
    staged, _ = delivery._notice_message(_table_rows(sdb)[0])
    assert pa.commit_delivery(sdb, PRODUCER, SID, staged)[0] == "written"
    sdb.append_message(SID, "assistant", "The build finished cleanly.")  # the turn ran; the process then died
    assert delivery.deliver_pending("ui-2", _session()) == "settled"
    assert turns["submitted"] == [] and len(_notices(sdb)) == 1
    assert pa.read_producer_admission(producer=PRODUCER, profile_home=_root())["status"] == "dispatched"


def test_two_deliverers_race_one_row(sdb):
    _admit()
    row = _table_rows(sdb)[0]
    a, _ = delivery._notice_message(row)
    b, _ = delivery._notice_message(row)
    assert pa.commit_delivery(sdb, PRODUCER, SID, a)[0] == "written"
    assert pa.commit_delivery(sdb, PRODUCER, SID, b) == ("committed", a["_row_id"])
    assert len(_notices(sdb)) == 1


# -- routing fences ---------------------------------------------------------------------------------------------

def test_routed_session_goes_to_admit_automation(sdb):
    sdb.create_session("gw-1", "desktop", session_key="agent:main:desktop:dm:x")
    receipt = _admit(sid="gw-1")
    assert receipt["outcome"] == "routed" and receipt["reason"] == "gateway_route"
    assert _table_rows(sdb) == []


def test_adopted_after_admission_is_rerouted_not_delivered(sdb, turns):
    from hermes_state_local_migration import LEGACY_PREFIX
    _admit()
    sdb._execute_write(lambda conn: conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)",
                                                 (LEGACY_PREFIX + SID, "{}")))
    assert delivery.deliver_pending("ui-1", _session()) in ("settled", "refused")
    assert _notices(sdb) == [] and turns["submitted"] == []
    receipt = pa.read_producer_admission(producer=PRODUCER, profile_home=_root())
    assert receipt["outcome"] == "routed" and receipt["reason"] == "local_receipt"


# -- producer authorization -------------------------------------------------------------------------------------

@pytest.mark.parametrize("producer", ['["viewer","x"]', "user-terminal:job-1:final", '["user-terminal"]',
                                      '[ "user-terminal", "job-1" ]', ""])
def test_only_trusted_producer_identities(sdb, producer):
    with pytest.raises(ValueError):
        _admit(producer=producer)


@pytest.mark.parametrize("text", ["", "   ", "x" * (pa.MAX_TEXT_CHARS + 1), "a\x00b"])
def test_text_is_bounded(sdb, text):
    with pytest.raises(ValueError):
        _admit(text=text)


# -- trust classification ---------------------------------------------------------------------------------------

def test_producer_text_is_not_expanded_as_context_references(monkeypatch):
    """Terminal output containing ``@path`` stays data: no file is pulled into the model context."""
    class _Stop(Exception):
        pass

    def _expand(*_a, **_k):
        raise AssertionError("producer text must not be @-expanded")

    def _images(agent, prompt, images):
        raise _Stop(prompt)

    import agent.context_references as cr
    monkeypatch.setattr(cr, "preprocess_context_references", _expand)
    for name in ("_set_session_context", "_wire_callbacks", "_apply_pending_model_switch",
                 "_sync_agent_model_with_config", "_sync_agent_compression_with_config",
                 "_sync_agent_fallback_with_config", "_sync_agent_daybreak", "_sync_bot_capabilities",
                 "_adopt_out_of_band_turns",
                 "_stage_first_contact_onboarding_note", "_stage_onboarding_turn_note", "_register_session_cwd"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_profile_runtime_scope_tokens", lambda home: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: "/tmp")
    monkeypatch.setattr(server, "make_stream_renderer", lambda cols: None)
    monkeypatch.setattr(server, "_route_turn_images", _images)
    session = {"session_key": SID, "history_lock": threading.RLock(), "history": [], "agent": object()}
    st = server._TurnRun(session["agent"], None, None, receipt_committed=True)
    st.literal_text = True
    from tools.approval_context import reset_current_session_key
    try:
        with pytest.raises(_Stop) as stop:
            server._prepare_turn_input("ui-1", session, st, "see @/etc/passwd", ["img"])
    finally:  # the real turn's finally resets the scope it bound; this test stops before it
        if st.scopes.approval is not None:
            reset_current_session_key(st.scopes.approval)
    assert stop.value.args[0] == "see @/etc/passwd"


def test_the_chat_poller_drains_producer_completions(monkeypatch):
    """Wiring: the per-session notification poller offers its chat's queued completions every kanban poll."""
    stop = threading.Event()
    seen = []
    monkeypatch.setattr(server, "_notif_poll_kanban", lambda sid, session: None)
    monkeypatch.setattr(server, "_maybe_fire_tui_loop_tick", lambda sid, session: None)
    monkeypatch.setattr(server, "_maybe_fire_tui_heartbeat_tick", lambda sid, session: None)

    def _deliver(sid, session):
        seen.append(sid)
        stop.set()
        return "none"

    monkeypatch.setattr(delivery, "deliver_pending", _deliver)
    worker = threading.Thread(target=server._notification_poller_scoped_loop, args=(stop, "ui-9", _session()),
                              daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive() and seen == ["ui-9"]


def test_sweeper_starts_once(monkeypatch):
    started = []
    monkeypatch.setattr(delivery, "_sweeper_started", False)
    monkeypatch.setattr(delivery, "spawn_context_thread", lambda target, *, name, args=(): type(
        "T", (), {"start": lambda self: started.append(name)})())
    assert delivery.start_producer_admission_sweeper() is not None
    assert delivery.start_producer_admission_sweeper() is None
    assert started == ["producer-admission-sweeper"]
