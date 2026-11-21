"""Typing into a chat the gateway holds for its own background turn queues the prompt; Stop still ends that turn;
a wake refused by a backend that is being replaced retries in seconds. [daveey]

Hub 2026-11-21: chats 39be4c, 8d8534 and a30b were held by the gateway's own Kanban close-out / notice turns. The
user typed into each; the Desktop took the chat over (``auto_take_over``) and the gateway's turn died with
``session_turn_lease_lost``. Now a prompt for a chat whose ONLY other holder is the live gateway goes to that
gateway's FIFO (the RT-37 forward), and any other holder keeps the takeover.
"""

from __future__ import annotations

import threading

import pytest

from hermes_cli.active_sessions import SESSION_NOT_OWNED, ActiveSessionRefusal
from tui_gateway import gateway_background_holder as gbh
from tui_gateway import owner_forward
from tui_gateway import server

KEY = "20261112_002413_39be4c"
GATEWAY = 2786221
DESKTOP = 2801475


def _owner(pid):
    return {"holder": f"pid={pid}:pidns=4026531836:turn=t1:platform=tui", "conversation_id": KEY, "acquired_at": 1.0}


def _entry(pid, key=KEY):
    return {"session_id": key, "pid": pid, "lease_id": f"lease-{pid}"}


# -- who holds the chat ------------------------------------------------------------------------------------------


def test_the_gateway_alone_holding_the_chat_is_named():
    session = {"session_key": KEY}
    assert gbh.gateway_background_holder(session, foreign_owner=lambda s: _owner(GATEWAY),
                                         registry=lambda: [_entry(GATEWAY), _entry(99, "other-chat")],
                                         gateway_pid=lambda: GATEWAY) == GATEWAY
    # The registry lease alone (between the notice's admission and its turn lease) proves it too.
    assert gbh.gateway_background_holder(session, foreign_owner=lambda s: None, registry=lambda: [_entry(GATEWAY)],
                                         gateway_pid=lambda: GATEWAY) == GATEWAY


@pytest.mark.parametrize("owner, entries, live", [
    (_owner(DESKTOP), [], GATEWAY),                      # another Desktop backend writes the turn
    (_owner(GATEWAY), [_entry(DESKTOP)], GATEWAY),       # the gateway AND another window hold it
    (_owner(GATEWAY), [], None),                         # no verified live gateway
    ({"holder": "legacy-holder"}, [], GATEWAY),          # an unreadable holder string
    (None, [], GATEWAY),                                 # nobody else holds it
])
def test_any_other_holder_keeps_the_takeover(owner, entries, live):
    assert gbh.gateway_background_holder({"session_key": KEY}, foreign_owner=lambda s: owner,
                                         registry=lambda: entries, gateway_pid=lambda: live) is None


def test_an_unreadable_holder_keeps_the_takeover():
    def broken(_session):
        raise OSError("state.db is locked")

    assert gbh.gateway_background_holder({"session_key": KEY}, foreign_owner=broken, registry=list,
                                         gateway_pid=lambda: GATEWAY) is None
    assert gbh.gateway_background_holder({"session_key": ""}, foreign_owner=lambda s: _owner(GATEWAY),
                                         registry=list, gateway_pid=lambda: GATEWAY) is None


# -- prompt.submit -----------------------------------------------------------------------------------------------


def _refusal():
    return ActiveSessionRefusal("This chat is active in another Hermes window/terminal.", SESSION_NOT_OWNED)


@pytest.fixture
def background(monkeypatch):
    """A chat whose newest turn is a Kanban close-out, held by the live gateway; forwards are recorded."""
    sent = []
    state = {"holder": GATEWAY, "receipt": {"status": "queued", "admission_id": "adm-1"}, "raise": None}
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: "kanban_notice")
    monkeypatch.setattr(gbh, "gateway_background_holder", lambda session, **kw: state["holder"])

    def forward(sid, session, text, **kw):
        sent.append((sid, text))
        if state["raise"] is not None:
            raise state["raise"]
        return state["receipt"]

    monkeypatch.setattr(owner_forward, "forward_prompt", forward)
    state["sent"] = sent
    return state


def test_a_typed_prompt_is_queued_behind_the_gateways_background_turn(background):
    resp = server._slot_refusal("r", "s", {"session_key": KEY}, "typed here", {}, _refusal(), False)

    assert resp["result"] == {"status": "queued", "admission_id": "adm-1", "forwarded": True,
                              "queued_behind": "kanban_notice"}
    assert background["sent"] == [("s", "typed here")]


def test_an_unconfirmed_forward_is_never_taken_over(background):
    background["raise"] = owner_forward.OwnerUnconfirmed("no receipt")
    resp = server._slot_refusal("r", "s", {"session_key": KEY}, "typed here", {}, _refusal(), False)

    assert resp["error"]["code"] == 4090
    assert resp["error"]["data"] == {"reason": "SESSION_OWNER_UNCONFIRMED"}  # no auto_take_over: it may be queued


@pytest.mark.parametrize("exc", [owner_forward.OwnerNotRunning("stopped"), owner_forward.OwnerUnavailable("no"),
                                 ConnectionRefusedError("dial failed")])
def test_a_forward_that_queued_nothing_falls_back_to_the_takeover(background, exc):
    background["raise"] = exc
    resp = server._slot_refusal("r", "s", {"session_key": KEY}, "typed here", {}, _refusal(), False)

    assert resp["error"]["data"] == {"reason": SESSION_NOT_OWNED, "auto_take_over": True,
                                     "holder_turn_kind": "kanban_notice"}


def test_another_holder_keeps_the_takeover_and_forwards_nothing(background):
    background["holder"] = None
    resp = server._slot_refusal("r", "s", {"session_key": KEY}, "typed here", {}, _refusal(), False)

    assert resp["error"]["data"]["auto_take_over"] is True and background["sent"] == []


@pytest.mark.parametrize("session_extra, params", [
    ({"attached_images": ["/tmp/a.png"]}, {}),
    ({}, {"truncate_before_user_ordinal": 2}),
])
def test_a_message_the_fifo_cannot_take_keeps_the_takeover(background, session_extra, params):
    session = {"session_key": KEY, **session_extra}
    resp = server._slot_refusal("r", "s", session, "typed here", params, _refusal(), False)

    assert resp["error"]["data"]["auto_take_over"] is True and background["sent"] == []


def test_prompt_submit_answers_the_queued_receipt(background, monkeypatch):
    sid = "rt-background-queue"
    monkeypatch.setitem(server._sessions, sid, {
        "session_key": KEY, "history": [], "history_lock": threading.RLock(), "running": False,
        "attached_images": [], "profile_home": None, "transport": None})
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: _refusal())

    resp = server.handle_request({"id": "r1", "method": "prompt.submit",
                                  "params": {"session_id": sid, "text": "typed here"}})

    assert resp["result"]["forwarded"] is True and resp["result"]["status"] == "queued"
    assert background["sent"] == [(sid, "typed here")]


# -- Stop ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def idle_chat(monkeypatch):
    sid = "rt-background-stop"
    session = {"agent": None, "agent_ready": threading.Event(), "session_key": KEY, "history": [],
               "history_lock": threading.RLock(), "running": False}
    monkeypatch.setitem(server._sessions, sid, session)
    calls, done = [], threading.Event()

    def take_over(rid, params):
        calls.append(params)
        done.set()
        return {"result": {"status": "taken_over"}}

    monkeypatch.setitem(server._methods, "session.take_over", take_over)
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: "kanban_notice")
    monkeypatch.setattr(gbh, "gateway_background_holder", lambda session, **kw: GATEWAY)
    return sid, session, calls, done


def _stop(sid, origin):
    params = {"session_id": sid, **({"origin": origin} if origin else {})}
    return server.handle_request({"id": "1", "method": "session.interrupt", "params": params})


def test_stop_takes_the_chat_over_from_the_gateways_background_turn(idle_chat):
    sid, _session, calls, done = idle_chat
    resp = _stop(sid, "stop_button")

    assert resp["result"] == {"status": "interrupted", "background_turn": True}
    assert done.wait(5) and calls == [{"session_id": sid}]


@pytest.mark.parametrize("origin", ["delete", "rewind", "plugin:x", None])
def test_other_interrupts_never_end_the_background_turn(idle_chat, origin):
    sid, _session, calls, done = idle_chat
    resp = _stop(sid, origin)

    assert resp["result"].get("background_turn") is None
    assert not done.wait(0.2) and calls == []


def test_stop_of_this_backends_own_turn_is_the_ordinary_stop(idle_chat, monkeypatch):
    sid, session, calls, done = idle_chat
    session["running"] = True
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **k: None)
    resp = _stop(sid, "stop_button")

    assert resp["result"].get("background_turn") is None and not done.wait(0.2) and calls == []


def test_stop_with_another_holder_is_the_ordinary_stop(idle_chat, monkeypatch):
    sid, _session, calls, _done = idle_chat
    monkeypatch.setattr(gbh, "gateway_background_holder", lambda session, **kw: None)
    resp = _stop(sid, "stop_button")

    assert resp["result"].get("background_turn") is None and calls == []


# -- a wake refused by a backend that is being replaced ------------------------------------------------------------


@pytest.mark.parametrize("reasons, expected", [
    ({DESKTOP: "code-skew: boot abc behind def"}, "code-skew: boot abc behind def"),
    ({DESKTOP: "superseded", GATEWAY: None}, None),  # the gateway is a real owner
    ({}, None),
])
def test_retiring_owner_reason_needs_every_other_holder_retiring(reasons, expected):
    entries = [_entry(pid) for pid in reasons]
    got = gbh.retiring_owner_reason({"session_key": KEY}, foreign_owner=lambda s: None, registry=lambda: entries,
                                    reason_of=reasons.get)
    assert got == expected


@pytest.fixture
def wake(monkeypatch):
    session = {"session_key": KEY}
    state = {"free_after": 2, "tries": 0, "reason": "code-skew", "claims": 0, "releases": 0}
    monkeypatch.setattr(gbh, "retiring_owner_reason", lambda s, **kw: state["reason"])
    clock = {"t": 0.0}

    def claim_slot(sid, s, **kw):
        state["tries"] += 1
        return None if state["tries"] >= state["free_after"] else _refusal()

    def run():
        return gbh.wake_past_retiring_owner(
            "s", session, "chat-closeout", KEY,
            claim_turn=lambda s: state.__setitem__("claims", state["claims"] + 1) or True,
            release_turn=lambda s: state.__setitem__("releases", state["releases"] + 1),
            claim_slot=claim_slot, foreign_owner=lambda s: None,
            sleep=lambda dt: clock.__setitem__("t", clock["t"] + dt), monotonic=lambda: clock["t"])

    return run, state, clock


def test_a_wake_waits_out_a_retiring_owner_and_claims_the_chat(wake):
    run, state, clock = wake
    assert run() is None
    assert state["tries"] == 2 and state["claims"] == 2 and state["releases"] == 1  # the last claim is kept
    assert clock["t"] == 2 * gbh.RETIRING_OWNER_POLL_S


def test_a_retiring_owner_that_never_lets_go_is_busy_not_refused(wake):
    run, state, clock = wake
    state["free_after"] = 10 ** 9
    assert run() == "busy"
    assert clock["t"] <= gbh.RETIRING_OWNER_WAIT_S + gbh.RETIRING_OWNER_POLL_S and state["claims"] == state["releases"]


def test_a_live_owner_is_still_refused(wake):
    run, state, _clock = wake
    state["reason"] = None
    assert run() == "refused"
    assert state["tries"] == 0


def test_the_wake_path_uses_the_retiring_owner_wait(monkeypatch):
    """``_wake_session_turn`` hands a refused slot claim to ``wake_past_retiring_owner`` and returns its verdict."""
    session = {"session_key": KEY}
    import tui_gateway.automation_replay as ar
    import tui_gateway.wake_profiles as wp
    monkeypatch.setattr(ar, "retiring_backend_refuses", lambda label, key: False)
    monkeypatch.setattr(wp, "live_for_profiles", lambda key, profiles: ("s", session))
    monkeypatch.setattr(server, "_restart_completed_failed_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *a: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *a, **k: None)
    monkeypatch.setattr(server, "_notif_claim_turn", lambda s: True)
    monkeypatch.setattr(server, "_notif_release_turn", lambda s: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: _refusal())
    seen = []
    monkeypatch.setattr(gbh, "wake_past_retiring_owner", lambda *a, **k: seen.append(a[3]) or "busy")

    assert server._wake_session_turn(KEY, "notice", label="chat-closeout") == "busy"
    assert seen == [KEY]
