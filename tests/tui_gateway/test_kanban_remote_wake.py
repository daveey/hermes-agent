"""A remote card's notice wakes an idle chat, like a local card's, instead of only writing a row. [daveey]

2026-10-29, David-MBP chat 20261027_134353_5ab65c: hub cards t_01a1873edd84d3fc (done 15:23:42Z) and
t_01a18300e42c3364 (done 16:53:01Z) were written into the idle chat on time as passive ``kanban_notice`` rows
(15:24:56Z and 16:53:29Z, ledger delivered=1), but no turn ran until a LOCAL card's notice woke the chat at
18:58Z, 2-3.5 h later. The local unloaded sweep has woken the chat since 10-27 (``kanban_unloaded`` "a row is
not a turn"); the remote sweep in the same loop only ever wrote the row.

These tests drive the production sweeper loop (``kanban_unloaded._loop``) with the real replica, ledger and
state.db paths of the sandboxed home; only the agent turn (``wake_turn``) and the worker thread are faked.
"""

import contextlib
import json
import os
import sqlite3
import threading
import time

import pytest

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_unloaded as ku

CHAT = "20261027_134353_5ab65c"


@pytest.fixture
def home(tmp_path, monkeypatch):
    hub = tmp_path / "hub-state"
    hub.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(hub))
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", raising=False)
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()
    with SessionDB(get_hermes_home() / "state.db") as db:
        db.create_session(CHAT, "desktop")
        db.append_message(CHAT, "user", "start the cards")
        db.append_message(CHAT, "assistant", "started")
    yield hub
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()


def _publish(hub, cards, arrived_ago=60.0):
    """The macbook replica: synchronized (cursor == head), hub-owned cards linked to CHAT, first seen on this
    host ``arrived_ago`` seconds ago (the durable arrival time both consumers share)."""
    with sqlite3.connect(hub / "hub.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS replica(gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, "
                     "data TEXT, updated_at INTEGER, PRIMARY KEY (gw, coll, key))")
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                         [("gateway", "macbook"), ("cursor", "100"), ("head", "100")])
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM replica").fetchone()[0]
        for card in cards:
            seq += 1
            conn.execute("INSERT OR REPLACE INTO replica VALUES ('hub','tasks',?,1,?,?,0)",
                         (f"default/default/{card['id']}", seq, json.dumps(card)))
    from tui_gateway.kanban_remote_observations import observe
    observe(hub / "hub.db", None, time.time() - arrived_ago)
    krn._reset_for_tests()  # a fresh process: only the durable ledger carries the arrival


def _done(tid, at, sessions=(CHAT,), summary="shipped the fix"):
    return {"id": tid, "status": "done", "title": f"card {tid}", "board": "default", "assignee": "default",
            "completed_at": at, "completion_notice": {"event_id": 81053, "created_at": at,
                                                      "payload": {"summary": summary}},
            "sessions": [{"gw": "macbook", "profile": "default", "session": s, "rel": "shepherd"} for s in sessions]}


def _blocked_on_david(tid, at):
    return {"id": tid, "status": "blocked", "title": f"card {tid}", "board": "default", "assignee": "default",
            "block_kind": "needs_input", "run": {"ended_at": at, "summary": "pick a window"},
            "human_request": {"action": "Reply A or B. Unblock = A; archive = drop.", "why_human": "owner pick",
                              "interaction": "reply", "scope": "deploy window", "evidence": "no window chosen"},
            "sessions": [{"gw": "macbook", "profile": "default", "session": CHAT, "rel": "shepherd"}]}


def _turn_writes_row(tip, text, metadata):
    """What the real woken turn persists on its thread (``prompt_turn._invoke_agent``): its user row, typed
    ``kanban_notice`` with the wake's display metadata."""
    with SessionDB(get_hermes_home() / "state.db") as db:
        db.append_message(tip, "user", text, display_kind="kanban_notice", display_metadata=metadata)


def _one_pass(monkeypatch, wake_verdict="started", row_lands=True):
    """One iteration of the production sweeper loop; returns the faked wakes it requested. ``row_lands`` makes
    a started turn persist its notice row before the waker hears "started" (False: admitted, row not yet
    written, as when the backend dies between admission and the turn thread's write)."""
    wakes = []

    def wake(tip, text, metadata):
        wakes.append((tip, text, metadata))
        if wake_verdict == "started" and row_lands:
            _turn_writes_row(tip, text, metadata)
        return wake_verdict

    monkeypatch.setattr(ku, "wake_turn", wake)
    monkeypatch.setattr(ku, "_spawn_thread", lambda job: (job(), "dispatched")[1])
    stop = threading.Event()
    monkeypatch.setattr(ku, "sweep_once", lambda *a, **k: [])
    host_pass = ku.host_pass

    def one_host_pass(*args, **kwargs):
        try:
            return host_pass(*args, **kwargs)
        finally:
            # Empty local sweeps may be skipped; stop at the host boundary.
            stop.set()

    monkeypatch.setattr(ku, "host_pass", one_host_pass)
    ku._loop(stop)
    return wakes


def _notice_rows():
    with SessionDB(get_hermes_home() / "state.db") as db:
        return [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]


def _ledger(ref):
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM notices WHERE session=? AND ref=?", (CHAT, ref)).fetchone()
        return dict(row) if row else None


def test_remote_done_notice_wakes_an_idle_chat_once(home, monkeypatch):
    """Late-delivery path: the sweep starts a turn with the notice instead of leaving a silent row."""
    now = time.time()
    _publish(home, [_done("t_01a1873edd84d3fc", now - 75)])

    wakes = _one_pass(monkeypatch)

    assert [w[0] for w in wakes] == [CHAT]
    assert "t_01a1873edd84d3fc" in wakes[0][1] and "shipped the fix" in wakes[0][1]
    assert wakes[0][2]["kanban_event_keys"] == ["remote:hub:default/default/t_01a1873edd84d3fc:81053"]
    assert wakes[0][2]["woken"] is True
    assert len(_notice_rows()) == 1  # the started turn's own row records the notice; no passive duplicate
    assert _ledger("hub/default/default/t_01a1873edd84d3fc")["delivered"] == 1
    # Next pass (and a restarted backend): nothing is woken or written again.
    assert _one_pass(monkeypatch) == []
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()
    assert _one_pass(monkeypatch) == []


def test_remote_notice_gives_a_live_poller_the_grace_window_first(home, monkeypatch):
    """A state that arrived seconds ago belongs to the chat's live poller (any backend): the sweep neither
    claims it nor writes a row until the grace window has passed."""
    now = time.time()
    _publish(home, [_done("t_young", now - 2)], arrived_ago=2)

    assert _one_pass(monkeypatch) == []
    assert _notice_rows() == []
    assert _ledger("hub/default/default/t_young") is None
    # The poller can still claim it.
    got = krn.collect(CHAT, "default", now=now)
    assert [item["ref"] for item in got] == ["hub/default/default/t_young"]


def test_refused_wake_falls_back_to_one_passive_row(home, monkeypatch):
    now = time.time()
    _publish(home, [_done("t_refused", now - 120)])

    wakes = _one_pass(monkeypatch, wake_verdict="refused")

    assert len(wakes) == 1
    rows = _notice_rows()
    assert len(rows) == 1 and "t_refused" in rows[0]["content"]
    assert _ledger("hub/default/default/t_refused")["delivered"] == 1
    assert _one_pass(monkeypatch, wake_verdict="refused") == []
    assert len(_notice_rows()) == 1


def test_card_blocked_on_david_writes_the_row_without_waking(home, monkeypatch):
    now = time.time()
    _publish(home, [_blocked_on_david("t_needs_david", now - 120)])

    assert _one_pass(monkeypatch) == []
    rows = _notice_rows()
    assert len(rows) == 1 and "t_needs_david" in rows[0]["content"]


# [daveey] "started" means the turn was ADMITTED (``_notif_submit``); its notice row is written later, on the
# turn thread. The waker acked the ledger at admission, so a backend that died in between left delivered=1
# (DELIVERED = "in the transcript") with no row: the failure class of ledger row 1245 (t_01a18bf35767bcd9).

def _orphan(ref):
    from tests.tui_gateway.test_kanban_remote_notices import _dead_pid
    with sqlite3.connect(krn.ledger_path()) as conn:
        conn.execute("UPDATE notices SET holder_pid=?, holder_start=NULL WHERE session=? AND ref=?",
                     (_dead_pid(), CHAT, ref))


def _swept_reason(notice_id):
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        row = conn.execute("SELECT reason FROM swept WHERE session=? AND notice_id=?", (CHAT, notice_id)).fetchone()
        return row[0] if row else None


def test_started_turn_whose_row_is_not_written_is_not_acked(home, monkeypatch):
    """RED before the fix: admission alone acked delivered=1 with no transcript row."""
    now = time.time()
    ref = "hub/default/default/t_admitted"
    _publish(home, [_done("t_admitted", now - 90)])

    wakes = _one_pass(monkeypatch, row_lands=False)

    assert len(wakes) == 1 and _notice_rows() == []
    row = _ledger(ref)
    assert row["delivered"] == 0 and row["holder_pid"] == os.getpid()  # still in flight, held by this backend
    assert _swept_reason(wakes[0][2]["notice_id"]) is None
    # The claim's live holder is this process, so the next pass neither wakes the chat again nor writes a row.
    assert _one_pass(monkeypatch, row_lands=False) == []
    assert _notice_rows() == [] and _ledger(ref)["delivered"] == 0


def test_started_turn_is_acked_by_the_next_pass_once_its_row_lands(home, monkeypatch):
    """The turn thread writes the row after admission: the next sweep pass acks DELIVERED, with no second wake."""
    now = time.time()
    ref = "hub/default/default/t_late_row"
    _publish(home, [_done("t_late_row", now - 90)])
    wakes = _one_pass(monkeypatch, row_lands=False)
    assert _ledger(ref)["delivered"] == 0

    _turn_writes_row(*wakes[0])  # the turn thread's write

    assert _one_pass(monkeypatch) == []
    assert len(_notice_rows()) == 1
    row = _ledger(ref)
    assert row["delivered"] == krn.DELIVERED and row["holder_pid"] is None
    assert _swept_reason(wakes[0][2]["notice_id"]) == "delivered"


def test_live_poller_acks_a_started_claim_once_its_row_lands(home, monkeypatch):
    """A woken chat is live in this backend, so the sweep skips it; its poller (``collect``) acks the claim this
    process holds once the key is in the transcript, and offers nothing new."""
    now = time.time()
    ref = "hub/default/default/t_live_ack"
    _publish(home, [_done("t_live_ack", now - 90)])
    wakes = _one_pass(monkeypatch, row_lands=False)
    _turn_writes_row(*wakes[0])

    with SessionDB(get_hermes_home() / "state.db") as db:
        got = krn.collect(CHAT, "default", now=time.time(),
                          transcript_has=lambda key, tid: krn.notice_in_transcript(db, CHAT, key, tid))

    assert got == []
    assert _ledger(ref)["delivered"] == krn.DELIVERED
    assert len(_notice_rows()) == 1


def test_backend_dies_before_the_started_turns_row_is_redelivered(home, monkeypatch):
    """At-least-once: the admitting backend dies before the turn writes its row. A successor finds the dead
    holder, the key is not in the transcript, so it re-offers (wakes again) instead of acking."""
    now = time.time()
    ref = "hub/default/default/t_died"
    _publish(home, [_done("t_died", now - 90)])
    assert len(_one_pass(monkeypatch, row_lands=False)) == 1
    _orphan(ref)
    krn._reset_for_tests()  # the successor process
    ku._reset_wake_budget_for_tests()

    wakes = _one_pass(monkeypatch)

    assert len(wakes) == 1
    assert len(_notice_rows()) == 1
    assert _ledger(ref)["delivered"] == krn.DELIVERED


def test_started_turn_whose_row_landed_is_acked_in_the_same_pass(home, monkeypatch):
    """Control: when the row is already durable as the waker hears "started", the ack is immediate."""
    now = time.time()
    ref = "hub/default/default/t_fast_row"
    _publish(home, [_done("t_fast_row", now - 90)])

    wakes = _one_pass(monkeypatch, row_lands=True)

    assert len(wakes) == 1 and len(_notice_rows()) == 1
    assert _ledger(ref)["delivered"] == krn.DELIVERED
    assert _swept_reason(wakes[0][2]["notice_id"]) == "delivered"


# [daveey] A woken turn can END without ever writing its notice row while this backend stays up
# (``_prepare_turn_input`` refusing the context, an exception before the persist). The claim's holder is this
# live process, so neither the holder-is-me ack nor the dead-holder re-offer ever fired: delivered=0 until the
# backend exited, hours later (t_01a18c287684accd). The claim now follows the woken turn: once that chat has no
# running turn and the key is still not in the transcript, this process re-offers it. A turn that is still
# running is left alone (no wall-clock timeout: a slow turn's late row must not be duplicated).

@pytest.fixture
def live_chat(monkeypatch):
    """The woken chat as ``_wake_session_turn`` leaves it: live in this process, its turn admitted (running)."""
    from tui_gateway import server
    sessions = {}

    def admit(tip):
        session = sessions.setdefault(tip, {"session_key": tip, "history_lock": threading.Lock()})
        session["running"] = True
        with server._sessions_lock:
            server._sessions[f"live-{tip}"] = session
        return session

    yield admit, sessions
    with server._sessions_lock:
        for tip in sessions:
            server._sessions.pop(f"live-{tip}", None)


def _one_pass_live(monkeypatch, admit, row_lands=False):
    """A sweep pass whose wake admits a live turn (running) in this process, like the real ``_wake_session_turn``."""
    wakes = []

    def wake(tip, text, metadata):
        wakes.append((tip, text, metadata))
        admit(tip)
        if row_lands:
            _turn_writes_row(tip, text, metadata)
        return "started"

    monkeypatch.setattr(ku, "wake_turn", wake)
    monkeypatch.setattr(ku, "_spawn_thread", lambda job: (job(), "dispatched")[1])
    stop = threading.Event()
    monkeypatch.setattr(ku, "sweep_once", lambda *a, **k: [])
    host_pass = ku.host_pass

    def one_host_pass(*args, **kwargs):
        try:
            return host_pass(*args, **kwargs)
        finally:
            # Empty local sweeps may be skipped; stop at the host boundary.
            stop.set()

    monkeypatch.setattr(ku, "host_pass", one_host_pass)
    ku._loop(stop)
    return wakes


def _poll(now=None):
    with SessionDB(get_hermes_home() / "state.db") as db:
        return krn.collect(CHAT, "default", now=now or time.time(),
                           transcript_has=lambda key, tid: krn.notice_in_transcript(db, CHAT, key, tid))


def test_woken_turn_that_ended_without_its_row_is_reoffered_to_the_live_poller(home, monkeypatch, live_chat):
    """RED before: the turn ended with no row, the chat stays live here, and the claim sat in flight."""
    admit, sessions = live_chat
    ref = "hub/default/default/t_refused_ctx"
    _publish(home, [_done("t_refused_ctx", time.time() - 90)])
    assert len(_one_pass_live(monkeypatch, admit)) == 1
    assert _ledger(ref)["delivered"] == 0

    sessions[CHAT]["running"] = False  # the turn ended ("Context injection refused") without persisting the row

    got = _poll()
    assert [item["ref"] for item in got] == [ref]
    assert got[0]["event_key"] == "remote:hub:default/default/t_refused_ctx:81053"
    assert _ledger(ref)["delivered"] == 0 and _ledger(ref)["holder_pid"] == os.getpid()
    # One re-offer: the next poll (the redelivery now in flight) does not offer it again.
    assert _poll() == []


def test_woken_turn_that_ended_without_its_row_is_rewoken_by_the_next_sweep(home, monkeypatch, live_chat):
    """Same gap when the chat is no longer live here: the next sweep pass in the same process wakes it again, and
    the second turn's row is acked: one row, DELIVERED."""
    admit, sessions = live_chat
    ref = "hub/default/default/t_ended_unloaded"
    _publish(home, [_done("t_ended_unloaded", time.time() - 90)])
    assert len(_one_pass_live(monkeypatch, admit)) == 1
    sessions[CHAT]["running"] = False
    from tui_gateway import server
    with server._sessions_lock:
        server._sessions.pop(f"live-{CHAT}")  # the chat was unloaded after its turn

    wakes = _one_pass_live(monkeypatch, admit, row_lands=True)

    assert [w[0] for w in wakes] == [CHAT]
    assert len(_notice_rows()) == 1
    assert _ledger(ref)["delivered"] == krn.DELIVERED


def test_woken_turn_still_running_without_its_row_is_not_reoffered(home, monkeypatch, live_chat):
    """Control: the admitted turn is still running and has not written its row yet: no re-offer, no second wake,
    claim still in flight; once its row lands it is acked with exactly one row."""
    admit, sessions = live_chat
    ref = "hub/default/default/t_slow_turn"
    _publish(home, [_done("t_slow_turn", time.time() - 90)])
    wakes = _one_pass_live(monkeypatch, admit)
    assert len(wakes) == 1

    assert _poll() == []
    from tui_gateway import server
    with server._sessions_lock:
        server._sessions.pop(f"live-{CHAT}")  # even with the sweep looking, a running turn is not re-woken
    assert _one_pass_live(monkeypatch, admit) == []
    assert _ledger(ref)["delivered"] == 0 and _notice_rows() == []

    _turn_writes_row(*wakes[0])  # the slow turn's late write
    sessions[CHAT]["running"] = False
    assert _poll() == []
    assert len(_notice_rows()) == 1 and _ledger(ref)["delivered"] == krn.DELIVERED
