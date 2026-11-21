"""Typing into a chat the gateway holds for its own background turn queues the prompt; Stop still ends that turn;
a wake refused by a backend that is being replaced retries in seconds. [daveey]

Hub 2026-11-21: chats 39be4c, 8d8534 and a30b were held by the gateway's own Kanban close-out / notice turns. The
user typed into each; the Desktop took the chat over (``auto_take_over``) and the gateway's turn died with
``session_turn_lease_lost``. Now a prompt for a chat whose ONLY other holder is the live gateway goes to that
gateway's FIFO (the RT-37 forward), and any other holder keeps the takeover.
"""

from __future__ import annotations

import threading
import time

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


def _plain_pids(key, owner, entries, me):
    """``holder_pids`` with every identity taken at face value (the identity checks have their own tests)."""
    return gbh.holder_pids(key, owner, entries, me, durable_pid=lambda h: gbh._holder_pid(h) or 0,
                           registry_pid=lambda e: int(e.get("pid") or 0))


def _dialed(pid):
    return lambda session: gbh.DialedGateway(pid, "endpoint") if pid else None


# -- who holds the chat ------------------------------------------------------------------------------------------


def test_the_gateway_alone_holding_the_chat_is_named():
    session = {"session_key": KEY}
    found = gbh.gateway_background_holder(session, foreign_owner=lambda s: _owner(GATEWAY),
                                          registry=lambda: [_entry(GATEWAY), _entry(99, "other-chat")],
                                          dialed=_dialed(GATEWAY), holders=_plain_pids)
    assert found == gbh.DialedGateway(GATEWAY, "endpoint")
    # The registry lease alone (between the notice's admission and its turn lease) proves it too.
    assert gbh.gateway_background_holder(session, foreign_owner=lambda s: None, registry=lambda: [_entry(GATEWAY)],
                                         dialed=_dialed(GATEWAY), holders=_plain_pids) is not None


@pytest.mark.parametrize("owner, entries, dialed_pid", [
    (_owner(DESKTOP), [], GATEWAY),                      # another Desktop backend writes the turn
    (_owner(GATEWAY), [_entry(DESKTOP)], GATEWAY),       # the gateway AND another window hold it
    (_owner(GATEWAY), [], None),                         # no ready gateway for this chat's profile
    (_owner(GATEWAY), [], 4242),                         # the forward would dial ANOTHER gateway
    ({"holder": "legacy-holder"}, [], GATEWAY),          # an unreadable holder string
    (None, [], GATEWAY),                                 # nobody else holds it
])
def test_any_other_holder_keeps_the_takeover(owner, entries, dialed_pid):
    assert gbh.gateway_background_holder({"session_key": KEY}, foreign_owner=lambda s: owner,
                                         registry=lambda: entries, dialed=_dialed(dialed_pid),
                                         holders=_plain_pids) is None


def test_an_unreadable_holder_keeps_the_takeover():
    def broken(_session):
        raise OSError("state.db is locked")

    assert gbh.gateway_background_holder({"session_key": KEY}, foreign_owner=broken, registry=list,
                                         dialed=_dialed(GATEWAY), holders=_plain_pids) is None
    assert gbh.gateway_background_holder({"session_key": ""}, foreign_owner=lambda s: _owner(GATEWAY),
                                         registry=list, dialed=_dialed(GATEWAY), holders=_plain_pids) is None


def test_a_durable_holder_from_another_pid_namespace_is_not_that_process(monkeypatch):
    import hermes_state_pidns
    monkeypatch.setattr(hermes_state_pidns, "holder_pid_checkable", lambda holder: False)
    assert gbh._durable_holder_pid(_owner(GATEWAY)["holder"]) == 0
    monkeypatch.setattr(hermes_state_pidns, "holder_pid_checkable", lambda holder: True)
    assert gbh._durable_holder_pid(_owner(GATEWAY)["holder"]) == GATEWAY
    assert gbh._durable_holder_pid("legacy-holder") == 0


def test_a_registry_lease_counts_only_for_this_hosts_live_process(monkeypatch):
    import os

    import hermes_cli.active_sessions as act
    me = os.getpid()
    here = act.owner_host()
    start = act._own_start_time()
    assert gbh._registry_pid({"pid": me, "host": here, "process_start_time": start}) == me
    assert gbh._registry_pid({"pid": me, "host": "another-host", "process_start_time": start}) == 0
    monkeypatch.setattr(act, "_pid_liveness", lambda pid, start=None, **kw: False)  # dead, or the pid was reused
    assert gbh._registry_pid({"pid": me, "host": here, "process_start_time": 1.0}) == 0
    assert gbh._registry_pid({"pid": "x"}) == 0


# -- prompt.submit -----------------------------------------------------------------------------------------------


def _refusal():
    return ActiveSessionRefusal("This chat is active in another Hermes window/terminal.", SESSION_NOT_OWNED)


@pytest.fixture
def background(monkeypatch):
    """A chat whose newest turn is a Kanban close-out, held by the live gateway; forwards are recorded."""
    sent = []
    state = {"holder": GATEWAY, "receipt": {"status": "queued", "admission_id": "adm-1"}, "raise": None}
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: "kanban_notice")
    monkeypatch.setattr(gbh, "gateway_background_holder",
                        lambda session, **kw: state["holder"] and gbh.DialedGateway(state["holder"], "endpoint"))

    def forward(sid, session, text, **kw):
        sent.append((sid, text))
        state["endpoint"] = kw.get("endpoint")
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
    assert background["endpoint"] == "endpoint"  # the very gateway proved to hold the chat is dialled


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

AUTO_HOLDER = f"pid={GATEWAY}:pidns=4026531836:turn=t1:auto=kanban_notice:platform=tui"


class _Endpoint:
    def __init__(self, capable=True):
        from agent.turn_end_request import CAPABILITY
        self.capabilities = frozenset({"session-authority-v1", *([CAPABILITY] if capable else [])})


class _DB:
    def __init__(self, state):
        self.state = state

    def request_turn_end(self, key, holder, reason=""):
        self.state["requests"].append((key, holder))
        return self.state["recorded"]


@pytest.fixture
def idle_chat(monkeypatch):
    import contextlib
    sid = "rt-background-stop"
    session = {"agent": None, "agent_ready": threading.Event(), "session_key": KEY, "history": [],
               "history_lock": threading.RLock(), "running": False}
    monkeypatch.setitem(server._sessions, sid, session)
    state = {"calls": [], "requests": [], "recorded": True, "events": [], "workers": [], "capable": True,
             "holder": AUTO_HOLDER, "answer": {"result": {"status": "taken_over"}}, "ordinary": []}
    monkeypatch.setattr(server, "_interrupt_session_turn", lambda *a, **k: state["ordinary"].append(a[0]))

    def take_over(rid, params):
        state["calls"].append(params)
        return state["answer"]

    @contextlib.contextmanager
    def session_db(s):
        yield _DB(state)

    monkeypatch.setitem(server._methods, "session.take_over", take_over)
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: "kanban_notice")
    monkeypatch.setattr(server, "_canonical_ledger_refusal", lambda session: None)
    monkeypatch.setattr(server, "_foreign_turn_owner", lambda session: {"holder": state["holder"]})
    monkeypatch.setattr(server, "_session_db", session_db)
    monkeypatch.setattr(gbh, "gateway_background_holder",
                        lambda session, **kw: gbh.DialedGateway(GATEWAY, _Endpoint(state["capable"])))
    real_emit = server._emit
    monkeypatch.setattr(server, "_emit", lambda kind, s, payload=None: state["events"].append((kind, s, payload))
                        if kind == "error" else real_emit(kind, s, payload))
    import agent.memory_provider as mp

    class _Deferred:  # the worker runs when the test says so: Stop must answer without it
        def __init__(self, target, name=None):
            self.target = target

        def start(self):
            state["workers"].append(self.target)

    monkeypatch.setattr(mp, "spawn_context_thread", lambda target, name=None: _Deferred(target, name))
    return sid, session, state


def _stop(sid, origin):
    params = {"session_id": sid, **({"origin": origin} if origin else {})}
    return server.handle_request({"id": "1", "method": "session.interrupt", "params": params})


def _run_workers(state):
    return [job() for job in state.pop("workers")]


def test_stop_answers_at_once_and_asks_exactly_the_holders_turn_to_end(idle_chat):
    sid, _session, state = idle_chat
    resp = _stop(sid, "stop_button")

    assert resp["result"] == {"status": "interrupted", "background_turn": True}
    assert state["ordinary"] == [sid]  # the ordinary idle Stop ran too (automation replays, post-turn review)
    assert state["requests"] == [] and len(state["workers"]) == 1  # nothing discovered on the RPC thread
    assert _run_workers(state) == ["end_requested"]
    assert state["requests"] == [(KEY, AUTO_HOLDER)] and state["calls"] == [] and state["events"] == []


def test_stop_asks_for_the_end_even_with_a_prompt_queued(idle_chat, monkeypatch):
    sid, _session, state = idle_chat
    monkeypatch.setattr(server, "_canonical_ledger_refusal", lambda session: object())
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["end_requested"] and state["calls"] == []


def test_a_lost_compare_and_set_is_told_to_the_chat(idle_chat):
    sid, _session, state = idle_chat
    state["recorded"] = False  # the holder changed between discovery and the request
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["end_request_failed"]
    assert state["events"] and state["events"][0][0] == "error" and state["calls"] == []


def test_an_older_gateway_with_nothing_queued_is_taken_over(idle_chat):
    sid, _session, state = idle_chat
    state["capable"] = False
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["taken_over"]
    assert state["calls"] == [{"session_id": sid}] and state["requests"] == []


def test_an_older_gateway_takeover_failure_is_told_to_the_chat(idle_chat):
    sid, _session, state = idle_chat
    state["capable"] = False
    state["answer"] = {"error": {"code": 4090, "message": "writer busy"}}
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["takeover_failed"]
    assert "writer busy" in state["events"][0][2]["message"]


def test_stop_racing_a_queued_forwarded_admission_on_an_older_gateway_never_takes_over(idle_chat, monkeypatch):
    """The typed prompt already waits in an older gateway's FIFO: a take-over would race that queue, so nothing is
    ended and the chat is told the message runs when the background turn ends."""
    sid, _session, state = idle_chat
    state["capable"] = False
    monkeypatch.setattr(server, "_canonical_ledger_refusal", lambda session: object())
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["queued_not_interrupted"]
    assert state["calls"] == [] and state["requests"] == []
    assert state["events"][0][2]["message"] == server.BACKGROUND_STOP_QUEUED_MESSAGE


def test_a_typed_holder_is_never_asked_to_end(idle_chat):
    """A capable gateway stamps every automated turn: an unstamped one was typed, and is neither asked to end nor
    taken over."""
    sid, _session, state = idle_chat
    state["holder"] = f"pid={GATEWAY}:pidns=4026531836:turn=t1:platform=tui"
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["typed_turn"]
    assert state["requests"] == [] and state["calls"] == []
    assert state["events"][0][2]["message"] == server.BACKGROUND_STOP_TYPED_MESSAGE


def test_an_unstamped_turn_on_an_older_gateway_is_taken_over_as_before(idle_chat):
    sid, _session, state = idle_chat
    state["capable"] = False
    state["holder"] = f"pid={GATEWAY}:pidns=4026531836:turn=t1:platform=tui"  # an older core stamps nothing
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["taken_over"] and state["requests"] == []


def test_no_gateway_holder_ends_nothing(idle_chat, monkeypatch):
    sid, _session, state = idle_chat
    monkeypatch.setattr(gbh, "gateway_background_holder", lambda session, **kw: None)
    _stop(sid, "stop_button")

    assert _run_workers(state) == ["no_holder"] and state["calls"] == state["requests"] == []


@pytest.mark.parametrize("origin", ["delete", "rewind", "plugin:x", None])
def test_other_interrupts_run_the_ordinary_stop(idle_chat, origin):
    sid, _session, state = idle_chat
    resp = _stop(sid, origin)

    assert resp["result"] == {"status": "interrupted"} and state["ordinary"] == [sid] and state["workers"] == []


def test_stop_of_this_backends_own_turn_is_the_ordinary_stop(idle_chat):
    sid, session, state = idle_chat
    session["running"] = True
    resp = _stop(sid, "stop_button")

    assert resp["result"] == {"status": "interrupted"} and state["ordinary"] == [sid] and state["workers"] == []


def test_stop_on_a_chat_whose_newest_turn_was_typed_is_the_ordinary_stop(idle_chat, monkeypatch):
    sid, _session, state = idle_chat
    monkeypatch.setattr(server, "_automation_holder_kind", lambda session: None)
    resp = _stop(sid, "stop_button")

    assert resp["result"] == {"status": "interrupted"} and state["ordinary"] == [sid] and state["workers"] == []


# -- the forwarded prompt's turn waits for the background turn's lease ---------------------------------------------


def test_a_forwarded_turn_waits_for_the_background_turns_lease_and_runs_once_after(tmp_path):
    """The gateway runs a forwarded prompt as a gateway-hosted turn: it WAITS for a held turn lease
    (``refuses_held_lease_at_once`` is False with ``_gateway_session_key``) instead of failing, and gets the lease
    exactly once, right after the background turn releases it."""
    import os
    from types import SimpleNamespace

    from agent.turn_facade_lease import refuses_held_lease_at_once
    from hermes_state import SessionDB

    assert refuses_held_lease_at_once(SimpleNamespace(_gateway_session_key="agent:main:tui"), "tui") is False
    db = SessionDB(tmp_path / "state.db")
    db.create_session(KEY, "desktop")
    background = f"pid={os.getpid()}:turn=closeout:platform=tui"
    forwarded = f"pid={os.getpid()}:turn=forwarded:platform=tui"
    assert db.acquire_session_turn_lease(KEY, background, ttl_seconds=60, wait_seconds=0)
    got, waiting = [], threading.Event()

    def forwarded_turn():
        got.append(db.acquire_session_turn_lease(KEY, forwarded, ttl_seconds=60, wait_seconds=10,
                                                 poll_interval_seconds=0.02, on_wait=lambda elapsed: waiting.set()))

    worker = threading.Thread(target=forwarded_turn)
    worker.start()
    assert waiting.wait(5)  # it found the background turn holding the lease and waits for it
    assert got == []
    db.release_session_turn_lease(KEY, background)
    worker.join(10)

    assert got == [True] and db.session_turn_lease_owner(KEY)["holder"] == forwarded
    db.close()


# -- a wake refused by a backend that is being replaced ------------------------------------------------------------


@pytest.mark.parametrize("reasons, expected", [
    ({DESKTOP: "code-skew: boot abc behind def"}, "code-skew: boot abc behind def"),
    ({DESKTOP: "superseded", GATEWAY: None}, None),  # the gateway is a real owner
    ({}, None),
])
def test_retiring_owner_reason_needs_every_other_holder_retiring(reasons, expected):
    entries = [_entry(pid) for pid in reasons]
    got = gbh.retiring_owner_reason({"session_key": KEY}, foreign_owner=lambda s: None, registry=lambda: entries,
                                    reason_of=reasons.get, holders=_plain_pids)
    assert got == expected


def test_a_retiring_owner_answers_busy_at_once_with_a_short_retry(monkeypatch):
    monkeypatch.setattr(gbh, "retiring_owner_reason", lambda s, **kw: "code-skew")
    started = time.monotonic()
    assert gbh.wake_past_retiring_owner("s", {"session_key": KEY}, "chat-closeout", KEY,
                                        foreign_owner=lambda s: None, now=lambda: 1000.0) == "busy"
    assert time.monotonic() - started < 1  # never sleeps inside the caller's wake slot
    assert gbh.retiring_retry_at(KEY, now=lambda: 1001.0) == 1000.0 + gbh.RETIRING_OWNER_RETRY_S
    assert gbh.retiring_retry_at(KEY) is None  # consumed


def test_a_stale_retiring_record_is_ignored(monkeypatch):
    monkeypatch.setattr(gbh, "retiring_owner_reason", lambda s, **kw: "code-skew")
    gbh.wake_past_retiring_owner("s", {"session_key": KEY}, "chat-closeout", KEY, foreign_owner=lambda s: None,
                                 now=lambda: 1000.0)
    assert gbh.retiring_retry_at(KEY, now=lambda: 1000.0 + gbh.RETIRING_RECORD_MAX_AGE_S + 1) is None


def test_a_live_owner_is_still_refused(monkeypatch):
    monkeypatch.setattr(gbh, "retiring_owner_reason", lambda s, **kw: None)
    assert gbh.wake_past_retiring_owner("s", {"session_key": KEY}, "chat-closeout", KEY,
                                        foreign_owner=lambda s: None) == "refused"
    assert gbh.retiring_retry_at(KEY) is None


def test_the_wake_path_answers_with_the_retiring_owner_verdict(monkeypatch):
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
    released = []
    monkeypatch.setattr(server, "_notif_release_turn", lambda s: released.append(1))
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: _refusal())
    seen = []
    monkeypatch.setattr(gbh, "wake_past_retiring_owner", lambda *a, **k: seen.append(a[3]) or "busy")

    assert server._wake_session_turn(KEY, "notice", label="chat-closeout") == "busy"
    assert seen == [KEY] and released == [1]
