"""A finished async delegation reaches its parent chat without anyone reopening it. [daveey]

Hub incident (2026-11-11): async delegation ``deleg_5facc89f`` of desktop chat ``20261031_222913_405ade`` was
recovered ``unknown`` at 10:06:18Z; its notice reached the chat at 18:20:02Z. The parent was loaded in no backend
until 17:24, and no poller exists for an unloaded chat; restored (``startup_replay``) copies are also refused by a
chat loaded only headless; and from 17:24 the chat was held by one backend while the queued copy sat in another,
whose ``SESSION_NOT_OWNED`` retries kept the row "recently touched" so the holder's orphan sweep never re-offered
it. On 2026-11-12 the hub ledger still had four such rows pending 13-26 h with zero delivery attempts.

``tui_gateway.delegation_unloaded`` claims such a row (the ledger's atomic gate) and runs one turn in the parent.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sqlite3
import threading
import time

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB
from tools import async_delegation as ad
from tui_gateway import delegation_unloaded as du

CHAT = "20261031_222913_405ade"
DID = "deleg_5facc89f"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    ad._reset_for_tests()
    du._reset_for_tests()
    # Owner liveness: the row's owner pid (999999) is dead, as after the 10:06 backend restart.
    monkeypatch.setattr(ad, "_owner_liveness", lambda: (lambda pid, started: int(pid or 0) == os.getpid()))
    yield
    ad._reset_for_tests()
    du._reset_for_tests()


@pytest.fixture
def home(tmp_path):
    token = set_hermes_home_override(str(tmp_path))
    try:
        db = SessionDB(tmp_path / "state.db")
        db.create_session(CHAT, "desktop")
        db.append_message(CHAT, "user", "review the fix in the background")
        db.close()
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def _opener(home):
    @contextlib.contextmanager
    def opener(chat_id):
        db = SessionDB(home / "state.db")
        try:
            yield (db, db.get_compression_tip(chat_id) or chat_id) if db.get_session(chat_id) else (None, None)
        finally:
            db.close()
    return opener


def _completion(did=DID, *, chat=CHAT, ui="4907c592", completed_ago=8 * 3600.0, status="unknown"):
    """A terminal ledger row whose owner died, pending delivery: what recovery left for ``deleg_5facc89f``."""
    now = time.time()
    evt = {"type": "async_delegation", "delegation_id": did, "session_key": chat, "origin_ui_session_id": ui,
           "parent_session_id": chat, "goal": "review the fix", "status": status, "summary": None,
           "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
           "dispatched_at": now - completed_ago - 1500, "completed_at": now - completed_ago}
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            "INSERT INTO async_delegations (delegation_id, origin_session, origin_ui_session_id, parent_session_id, "
            "state, dispatched_at, completed_at, updated_at, delivery_state, delivery_attempts, owner_pid, "
            "owner_started_at, task_json, event_json, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, 999999, 1, '{}', ?, '{}')",
            (did, chat, ui, chat, status, evt["dispatched_at"], evt["completed_at"], evt["completed_at"],
             json.dumps(evt)))
    return evt


def _row(home, did=DID) -> dict:
    conn = sqlite3.connect(home / "state.db")
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id=?", (did,)).fetchone())
    finally:
        conn.close()


def _set(home, did=DID, **cols) -> None:
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(f"UPDATE async_delegations SET {', '.join(f'{k}=?' for k in cols)} WHERE delegation_id=?",
                     (*cols.values(), did))
        conn.commit()
    finally:
        conn.close()


class _Waker:
    def __init__(self, verdict="started"):
        self.verdict, self.calls = verdict, []

    def __call__(self, chat, text, metadata):
        self.calls.append((chat, text, metadata))
        return self.verdict


def _sweep(home, wake, *, holders=list, busy=lambda ids: False, now=None):
    return du.sweep_once(now=now, homes=lambda: [None], opener=_opener(home), holders=holders, busy=busy,
                         wake=wake, spawn=lambda job: job())


def test_unloaded_parent_gets_one_turn_and_the_row_is_delivered(home):
    _completion()
    wake = _Waker()

    decisions = _sweep(home, wake)

    assert [d["verdict"] for d in decisions] == ["started"]
    assert len(wake.calls) == 1
    chat, text, meta = wake.calls[0]
    assert chat == CHAT and DID in text
    assert meta["delegation_id"] == DID and meta["woken"] is True
    row = _row(home)
    assert row["delivery_state"] == "delivered" and row["delivery_claim"] is None
    # Exactly once: nothing left for the next pass, and the poller's late copy cannot claim it.
    assert _sweep(home, wake) == [] and len(wake.calls) == 1
    assert ad.claim_event_delivery({"type": "async_delegation", "delegation_id": DID}, "tui-poller") is None


def test_a_live_claim_wins_and_the_sweeper_never_doubles_it(home):
    _completion()
    assert ad.claim_completion_delivery(DID, "tui-poller:1:x")
    wake = _Waker()

    assert _sweep(home, wake) == []
    assert wake.calls == []
    assert _row(home)["delivery_claim"] == "tui-poller:1:x"


def test_a_claim_taken_between_select_and_claim_is_left_alone(home, monkeypatch):
    _completion()
    real = ad.claim_completion_delivery

    def racing(did, claim):
        assert real(did, "tui-poller:2:y")  # the poller got there first
        return real(did, claim)
    monkeypatch.setattr(ad, "claim_completion_delivery", racing)
    wake = _Waker()

    assert [d["verdict"] for d in _sweep(home, wake)] == ["skipped:claimed"]
    assert wake.calls == []


@pytest.mark.parametrize("verdict", ["busy", "refused", "error"])
def test_a_wake_that_did_not_start_defers_without_spending_an_attempt_and_backs_off(home, verdict):
    _completion()
    wake = _Waker(verdict)

    assert [d["verdict"] for d in _sweep(home, wake)] == [verdict]
    row = _row(home)
    assert row["delivery_state"] == "pending" and row["delivery_claim"] is None
    assert row["delivery_attempts"] == 0
    assert [d["verdict"] for d in _sweep(home, wake)] == ["skipped:backoff"]
    later = time.time() + du.RETRY_S + 1
    wake.verdict = "started"
    assert [d["verdict"] for d in _sweep(home, wake, now=later)] == ["started"]
    assert _row(home)["delivery_state"] == "delivered"


def test_a_chat_no_store_has_is_left_pending(home):
    _completion(chat="20261101_000000_gone00")
    wake = _Waker()

    assert [d["verdict"] for d in _sweep(home, wake)] == ["skipped:no-store"]
    assert wake.calls == [] and _row(home)["delivery_state"] == "pending"


@pytest.mark.parametrize("mark, verdict", [
    ({"archived": 1}, "skipped:archived"),
    ({"end_reason": "moved_to_gateway:macbook"}, "skipped:moved"),
])
def test_an_archived_or_moved_parent_is_never_woken(home, mark, verdict):
    """``session.resume`` refuses neither, so the pass itself must apply the kanban sweep's ``chat_skip_reason``
    gate: an archived chat must not be revived, and a chat moved to another gateway is no longer this host's."""
    _completion()
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(f"UPDATE sessions SET {', '.join(f'{k}=?' for k in mark)} WHERE id=?", (*mark.values(), CHAT))
        conn.commit()
    finally:
        conn.close()
    wake = _Waker()

    assert [d["verdict"] for d in _sweep(home, wake)] == [verdict]
    row = _row(home)
    assert wake.calls == [] and row["delivery_state"] == "pending" and row["delivery_claim"] is None
    assert row["delivery_attempts"] == 0  # unclaimed: an unarchived/reopened chat's poller can still take it


def test_dead_owner_recovery_runs_at_most_every_recover_every_s(home, monkeypatch):
    _completion()
    calls = []
    real = ad.unclaimed_pending_completions
    monkeypatch.setattr(ad, "unclaimed_pending_completions",
                        lambda **kw: calls.append(kw["recover"]) or real(**kw))
    t0 = time.time()

    for dt in (0.0, 30.0, du.RECOVER_EVERY_S - 1, du.RECOVER_EVERY_S + 1):
        _sweep(home, _Waker("busy"), now=t0 + dt)

    assert calls == [True, False, False, True]


def test_a_lapsed_claim_at_ack_is_logged_as_a_warning(home, monkeypatch, caplog):
    _completion()
    monkeypatch.setattr(ad, "complete_completion_delivery", lambda did, claim: False)

    with caplog.at_level("WARNING", logger=du.__name__):
        assert [d["verdict"] for d in _sweep(home, _Waker())] == ["started"]
    assert any("lease" in r.getMessage() and DID in r.getMessage() for r in caplog.records)


def test_a_chat_held_by_another_backend_is_left_to_it(home):
    _completion()
    wake = _Waker()
    other = [{"session_id": CHAT, "pid": os.getpid() + 1}]

    assert [d["verdict"] for d in _sweep(home, wake, holders=lambda: other)] == [
        f"skipped:held:pid {os.getpid() + 1}"]
    assert wake.calls == [] and _row(home)["delivery_claim"] is None
    # This backend holds the chat (a stale sibling lease beside it does not hand it away).
    mine = other + [{"session_id": CHAT, "pid": os.getpid()}]
    assert [d["verdict"] for d in _sweep(home, wake, holders=lambda: mine)] == ["started"]


def test_a_chat_mid_turn_here_waits_for_its_own_poller(home):
    _completion()
    wake = _Waker()

    assert [d["verdict"] for d in _sweep(home, wake, busy=lambda ids: CHAT in ids)] == ["skipped:busy"]
    assert wake.calls == []


def test_fresh_results_and_gateway_results_are_not_the_sweepers(home):
    _completion("deleg_fresh", completed_ago=5.0)
    _completion("deleg_gateway", ui="")
    wake = _Waker()

    assert [(d["delegation_id"], d["verdict"]) for d in _sweep(home, wake)] == [
        ("deleg_gateway", "skipped:not-a-ui-result")]
    assert wake.calls == []


def test_the_wake_budget_caps_turns_per_chat(home, monkeypatch):
    monkeypatch.setattr(du, "WAKES_PER_HOUR", 1)
    _completion("deleg_a")
    _completion("deleg_b")
    wake = _Waker()

    assert [d["verdict"] for d in _sweep(home, wake)] == ["started", "skipped:budget"]
    assert _row(home, "deleg_b")["delivery_state"] == "pending"


def test_tail_repro_copy_in_another_backend_starves_the_holders_orphan_sweep(home):
    """17:24-18:20: backend B holds the chat (David opened it there); backend A holds the queued copy and its turn
    is refused SESSION_NOT_OWNED, retried with backoff (<= 300 s), each retry claiming then deferring the row. B had
    offered the row itself at startup (its copy was refused before the chat was open there). B's orphan sweep then
    reads the row as "touched within a claim lease" on every pass and never re-offers it: the result waits until
    one backend lets go of the chat. The ledger-claimed wake in B delivers it on its next pass."""
    _completion()
    q_b: queue.Queue = queue.Queue()
    assert ad.restore_undelivered_completions(q_b) == 1  # B's startup replay ...
    ad.return_completion_offer(q_b.get_nowait())  # ... refused (no transport yet) and handed back
    start = time.time()
    for minute in range(0, 60, 1):  # an hour of B's sweeps, A retrying at its 300 s backoff ceiling
        now = start + minute * 60
        last_retry = now - (minute * 60) % 300
        _set(home, updated_at=last_retry)  # A's claim + defer_completion_delivery touch
        assert ad.sweep_orphaned_completions(q_b, now=now) == 0, f"re-offered at minute {minute}"
    assert q_b.empty() and _row(home)["delivery_state"] == "pending"

    wake = _Waker()
    mine = [{"session_id": CHAT, "pid": os.getpid()}]
    assert [d["verdict"] for d in _sweep(home, wake, holders=lambda: mine)] == ["started"]
    assert _row(home)["delivery_state"] == "delivered"
    # A's next retry finds the row settled: its claim fails, so it can never run the result a second time.
    assert not ad.claim_completion_delivery(DID, "tui-poller:A:retry")


def test_the_kanban_unloaded_loop_runs_the_delegation_pass(monkeypatch):
    from tui_gateway import kanban_unloaded as ku
    from tui_gateway import notice_fanout, notice_guarantee, reap_notices, server
    stop = threading.Event()
    ran = []
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.setattr(ku, "sweep_remote_once", lambda *a, **k: [])
    monkeypatch.setattr(ku, "sweep_once", lambda *a, **k: [])
    monkeypatch.setattr(ku, "live_session_ids", lambda: set())
    monkeypatch.setattr(notice_fanout, "card_change_pass", lambda *a, **k: None)
    monkeypatch.setattr(notice_guarantee, "guarantee_pass", lambda *a, **k: stop.set())
    monkeypatch.setattr(reap_notices, "retry_pending_killed_notices", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_db", lambda: None, raising=False)
    monkeypatch.setattr(du, "sweep_once", lambda *a, **k: ran.append(1) or [])

    ku._loop(stop)

    assert ran == [1]


def test_end_to_end_unloaded_parent_is_resumed_here_and_runs_the_result_turn(home, monkeypatch):
    """The real waker (``server._wake_session_turn``): the parent is in no backend, so it is resumed here and one
    turn runs with the formatted result, typed ``async_delegation_complete``; the row is then delivered."""
    from tui_gateway import server
    _completion()
    live: dict = {}
    submitted: list = []

    def resume(rid, params):
        assert params["session_id"] == CHAT and params["omit_messages"]
        ready = threading.Event()
        ready.set()
        live.update(sid="sid-woken", session={"history_lock": threading.RLock(), "running": False,
                                              "agent_ready": ready, "session_key": CHAT, "profile_home": None})
        return {"result": {"session_id": "sid-woken"}}

    monkeypatch.setitem(server._methods, "session.resume", resume)
    monkeypatch.setattr(server, "_find_live_session_by_key",
                        lambda key, profile_home=None: (live["sid"], live["session"]) if live and key == CHAT else None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda sid, session: None)
    monkeypatch.setattr(server, "_notif_submit",
                        lambda rid, sid, session, text, what, **kw: submitted.append((sid, text, kw)) or True)

    decisions = du.sweep_once(homes=lambda: [None], opener=_opener(home), holders=list,
                              busy=lambda ids: False, spawn=lambda job: job())

    assert [d["verdict"] for d in decisions] == ["started"]
    assert len(submitted) == 1
    sid, text, kw = submitted[0]
    assert sid == "sid-woken" and DID in text
    assert kw["display_kind"] == "async_delegation_complete" and kw["display_metadata"]["delegation_id"] == DID
    assert _row(home)["delivery_state"] == "delivered"
