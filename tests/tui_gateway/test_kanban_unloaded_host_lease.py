"""Unloaded sweeper + producer admission under the host loop lease (design t_01a1d45d75851804 §2b). [daveey]

Every gateway, Desktop serve and dashboard process ran the whole unloaded sweep and the cold-chat admission sweep
(``kanban_unloaded._loop`` = 14.9% of a steady hub serve's samples, ``producer_admission._loop`` 3.9%). Now one
lease holder runs the host pass; every other process runs a residual pass for the chats it holds itself, because the
delegation and guarantee passes skip a chat held by another backend (``skipped:held``).
"""

from __future__ import annotations

import contextlib
import importlib
import time

import pytest

from hermes_cli import host_loop_lease as hll
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.tui_gateway import test_delegation_unloaded_wake as dw
from tests.tui_gateway import test_kanban_unloaded_wake as kw
from tests.tui_gateway import test_notice_guarantee as gw
from tests.tui_gateway import test_session_outcome_notices as so_helpers
from tui_gateway import delegation_unloaded as du
from tui_gateway import kanban_unloaded as ku
from tui_gateway import notice_fanout as nf
from tui_gateway import notice_guarantee as ng
from tui_gateway import producer_admission as pa
from tui_gateway import reap_notices as rn
from tui_gateway import wake_slots

db = so_helpers.db  # session-outcome fixture (card_change_pass)
_clean_state = dw._clean_state  # autouse: delegation ledger reset and dead-owner liveness, as in its home module


@pytest.fixture(autouse=True)
def _lease_on(monkeypatch):
    monkeypatch.delenv(hll.KILL_SWITCH_ENV, raising=False)
    monkeypatch.delenv(wake_slots.HOLDER_SLOTS_ENV, raising=False)
    yield
    wake_slots.set_holder(False)


class _Lease:
    def __init__(self, held: bool):
        self._held, self.beats = held, 0

    def held(self) -> bool:
        return self._held

    def beat(self) -> None:
        self.beats += 1


def _record_passes(monkeypatch, live: frozenset[str] | set[str] = frozenset()):
    """Stub every pass of the loop; return the list of (pass, kwargs) calls."""
    ran = []
    monkeypatch.setattr(ku, "live_session_ids", lambda: set(live))
    monkeypatch.setattr(ku, "sweep_remote_once", lambda ids, **k: ran.append(("remote", None)) or [])
    monkeypatch.setattr(ku, "sweep_once", lambda ids, **k: ran.append(("local", None)) or [])
    monkeypatch.setattr(du, "sweep_once", lambda **k: ran.append(("delegation", k.get("only"))) or [])
    monkeypatch.setattr(nf, "card_change_pass", lambda **k: ran.append(("card_change", None)) or [])
    monkeypatch.setattr(rn, "retry_pending_killed_notices_all", lambda **k: ran.append(("killed", None)) or [])
    monkeypatch.setattr(ng, "guarantee_pass", lambda **k: ran.append(("guarantee", k.get("only"))) or [])
    return ran


# -- routing -----------------------------------------------------------------------------------------------------


def test_the_holder_runs_the_host_pass_and_beats(monkeypatch):
    ran = _record_passes(monkeypatch, live={"chat-here"})
    lease = _Lease(True)

    assert ku.run_pass(lease) == "host"

    assert [name for name, _ in ran] == ["remote", "local", "delegation", "card_change", "killed", "guarantee"]
    assert ("delegation", None) in ran and ("guarantee", None) in ran  # every chat, not only live ones
    assert lease.beats == 1


def test_a_standby_runs_only_the_residual_pass_for_its_live_chats(monkeypatch):
    ran = _record_passes(monkeypatch, live={"chat-here"})
    lease = _Lease(False)

    assert ku.run_pass(lease) == "residual"

    assert ran == [("delegation", {"chat-here"}), ("guarantee", {"chat-here"})]
    assert lease.beats == 0


def test_a_standby_with_no_live_chat_does_nothing(monkeypatch):
    ran = _record_passes(monkeypatch)
    assert ku.run_pass(_Lease(False)) == "residual" and ran == []


def test_kill_switch_runs_the_host_pass_in_every_process(monkeypatch, tmp_path):
    monkeypatch.setenv(hll.KILL_SWITCH_ENV, "0")
    ran = _record_passes(monkeypatch)
    a = hll.HostLoopLease(ku.LEASE_NAME, "serve", interval=30, home=tmp_path)
    b = hll.HostLoopLease(ku.LEASE_NAME, "dashboard", interval=30, home=tmp_path)

    assert ku.run_pass(a) == "host" and ku.run_pass(b) == "host"
    assert [name for name, _ in ran].count("local") == 2
    assert ku._wake_slots.limit == ku.MAX_PARALLEL_WAKES  # today's per-process capacity


def test_the_loop_releases_its_lease_when_stopped(monkeypatch, tmp_path):
    import threading
    _record_passes(monkeypatch)
    stop = threading.Event()
    lease = hll.HostLoopLease(ku.LEASE_NAME, "serve", interval=30, home=tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_FIRST_S", "0")
    monkeypatch.setattr(ku, "make_lease", lambda: lease)
    monkeypatch.setattr(ku, "run_pass", lambda lease_, grace=0: stop.set() or "host")
    ku._loop(stop)
    other = hll.HostLoopLease(ku.LEASE_NAME, "dashboard", interval=30, home=tmp_path)
    try:
        assert other.held()
    finally:
        other.release()


# -- exactly once ------------------------------------------------------------------------------------------------


def test_an_unloaded_chat_is_delivered_exactly_once_by_the_holder(monkeypatch, tmp_path):
    """Two processes' loops (two leases on one home): only the holder sweeps, so the chat gets one turn."""
    from hermes_state import SessionDB
    sdb = SessionDB(tmp_path / "state.db")
    sdb.create_session(kw.CHAT, "desktop")
    sdb.append_message(kw.CHAT, "user", "start the cards")
    ku._reset_wake_budget_for_tests()
    wake = kw._Wake("started")
    real_sweep = ku.sweep_once
    ran = _record_passes(monkeypatch)
    monkeypatch.setattr(ku, "sweep_once", lambda ids, **k: real_sweep(
        ids, now=kw.LATER, session_db=kw._opener(sdb), wake=wake, spawn=kw._inline, holders=list))
    kw._card()
    gateway = hll.HostLoopLease(ku.LEASE_NAME, "gateway", interval=30, home=tmp_path / "home")
    serve = hll.HostLoopLease(ku.LEASE_NAME, "serve", interval=30, home=tmp_path / "home")
    try:
        verdicts = [ku.run_pass(gateway), ku.run_pass(serve), ku.run_pass(gateway), ku.run_pass(serve)]
    finally:
        gateway.release()
        serve.release()
        sdb.close()
    assert verdicts == ["host", "residual", "host", "residual"]
    assert len(wake.calls) == 1 and wake.calls[0][0] == kw.CHAT
    assert [name for name, _ in ran].count("card_change") == 2  # host work once per tick, in one process


# -- residual deliveries for chats live in a non-holder ----------------------------------------------------------


def test_a_live_chat_in_a_standby_gets_its_delegation_from_its_own_residual_pass(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB
    token = set_hermes_home_override(str(tmp_path))
    try:
        sdb = SessionDB(tmp_path / "state.db")
        sdb.create_session(dw.CHAT, "desktop")
        sdb.append_message(dw.CHAT, "user", "review the fix in the background")
        sdb.close()
        dw._completion()
        wake = dw._Waker()

        def residual(only):
            return du.sweep_once(homes=lambda: [None], opener=dw._opener(tmp_path), holders=list,
                                 busy=lambda ids: False, wake=wake, spawn=lambda job: job(), only=only)

        assert [d["verdict"] for d in residual({"another-chat"})] == ["skipped:not-live-here"]
        assert wake.calls == [] and dw._row(tmp_path)["delivery_state"] == "pending"
        assert [d["verdict"] for d in residual({dw.CHAT})] == ["started"]
        assert len(wake.calls) == 1 and dw._row(tmp_path)["delivery_state"] == "delivered"
    finally:
        reset_hermes_home_override(token)


def test_a_live_chat_in_a_standby_gets_its_closeout_from_its_own_residual_pass(tmp_path):
    from hermes_state import SessionDB
    getattr(ng, "_reset_refreshed_for_tests", lambda: None)()
    sdb = SessionDB(tmp_path / "state.db")
    try:
        for chat in (gw.CHAT, "20261029_130000_other"):
            sdb.create_session(chat, "desktop")
            sdb.append_message(chat, "user", "run the deploy card")
            row = sdb.append_kanban_notice(chat, "✔ Kanban t_aaa done — deploy card",
                                           {"notice_id": f"n:{chat}", "display_text": "done"})
            sdb._write_rowcount("UPDATE messages SET timestamp = ? WHERE id = ?", (time.time() - 600, row))
        calls = []

        @contextlib.contextmanager
        def opener(chat_id):
            yield sdb, sdb.get_compression_tip(chat_id) or chat_id

        out = ng.guarantee_pass(stores=lambda: iter([sdb]), holders=list, session_db=opener,
                                wake=lambda sid, msg: calls.append(sid) or "started", spawn=lambda job: job(),
                                only={gw.CHAT})
    finally:
        sdb.close()
    assert calls == [gw.CHAT] and [o["session_id"] for o in out] == [gw.CHAT]


# -- killed-notice retry covers every store ------------------------------------------------------------------------


def test_the_host_pass_retries_killed_notices_in_every_profile_store(monkeypatch):
    seen = []
    monkeypatch.setattr(ku, "live_session_ids", set)
    monkeypatch.setattr(ku, "sweep_remote_once", lambda ids, **k: [])
    monkeypatch.setattr(ku, "sweep_once", lambda ids, **k: [])
    monkeypatch.setattr(du, "sweep_once", lambda **k: [])
    monkeypatch.setattr(nf, "card_change_pass", lambda **k: [])
    monkeypatch.setattr(ng, "guarantee_pass", lambda **k: [])
    monkeypatch.setattr(ng, "_stores", lambda: iter(["default-store", "work-store"]))
    monkeypatch.setattr(rn, "retry_pending_killed_notices", lambda store, **k: seen.append(store) or [])

    ku.host_pass()

    assert seen == ["default-store", "work-store"]


# -- wake slots ----------------------------------------------------------------------------------------------------


def test_the_holder_gets_eight_wake_slots_and_a_standby_its_base(monkeypatch):
    _record_passes(monkeypatch)
    ku.run_pass(_Lease(True))
    assert ku._wake_slots.limit == ng._wake_slots.limit == du._wake_slots.limit == wake_slots.HOLDER_SLOTS == 8
    monkeypatch.setenv(wake_slots.HOLDER_SLOTS_ENV, "3")
    ku.run_pass(_Lease(True))
    assert ku._wake_slots.limit == 3
    ku.run_pass(_Lease(False))
    assert ku._wake_slots.limit == ku.MAX_PARALLEL_WAKES and ng._wake_slots.limit == ng.MAX_PARALLEL_WAKES


def test_slot_exhaustion_rewinds_the_claim_and_retries_instead_of_a_passive_row(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    sdb = SessionDB(tmp_path / "state.db")
    sdb.create_session(kw.CHAT, "desktop")
    sdb.append_message(kw.CHAT, "user", "start the cards")
    ku._reset_wake_budget_for_tests()
    monkeypatch.setattr(ku, "_wake_slots", wake_slots.WakeSlots(1))
    assert ku._wake_slots.acquire()  # another chat's wake holds the only slot
    tid = kw._card()
    wake = kw._Wake("started")
    soon = time.time() + 60  # past the 30 s grace, far from OVERDUE_S
    try:
        out = ku.sweep_once(set(), now=soon, session_db=kw._opener(sdb), wake=wake, spawn=kw._inline, holders=list)
        assert out == [] and wake.calls == [] and kw._notices(sdb) == []  # no passive row
        ku._wake_slots.release()
        out = ku.sweep_once(set(), now=soon + 30, session_db=kw._opener(sdb), wake=wake, spawn=kw._inline,
                            holders=list)
    finally:
        sdb.close()
        ku._reset_wake_budget_for_tests()
    assert [o["wake_result"] for o in out] == ["started"] and len(wake.calls) == 1 and tid in wake.calls[0][1]


def test_a_remote_notice_with_no_free_slot_is_handed_back_until_overdue(monkeypatch):
    monkeypatch.setattr(ku, "_wake_slots", wake_slots.WakeSlots(0))
    monkeypatch.setattr(ku, "_slot_waits", {})
    ku._reset_wake_budget_for_tests()
    waker = ku._RemoteWaker(lambda *a: "started", lambda job: job(), 30.0)
    persisted, deferred = [], []
    task = {"id": "t_remote", "status": "done"}

    def deliver(now):
        return waker.deliver("chat", "✔ Kanban t_remote done", {"notice_id": "n1"}, task, now,
                             persist=lambda: persisted.append(now) or True, started=lambda: None,
                             defer=lambda: deferred.append(now))

    t0 = time.time()
    assert deliver(t0) == ku.SLOTS_DEFERRED and deliver(t0 + 60) == ku.SLOTS_DEFERRED
    assert persisted == [] and deferred == [t0, t0 + 60]
    assert deliver(t0 + ku.OVERDUE_S + 1) == "skipped:slots" and persisted == [t0 + ku.OVERDUE_S + 1]


# -- notice_fanout hand-off ------------------------------------------------------------------------------------------


def _new_process():
    """What a lease hand-off to another process looks like to notice_fanout: none of the old process's memory."""
    from tui_gateway import notice_fanout_state
    notice_fanout_state.forget_memory_for_tests()
    importlib.reload(nf)


def test_a_lease_hand_off_keeps_the_card_change_interval(db, monkeypatch):
    from hermes_cli import kanban_db_notify as kbn
    from hermes_cli import kanban_session_activity as ksa
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="deploy card", assignee="worker", session_id=so_helpers.CHAT)
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=so_helpers.CHAT)
    assert nf.card_change_pass(session_db=so_helpers._opener(db)) == []  # the old holder's pass seeds the cursor
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary="deployed")  # changes while the lease moves
    _new_process()
    ksa._reset_cache_for_tests()

    changed = nf.card_change_pass(session_db=so_helpers._opener(db))

    assert [(c["task_id"], c["status"]) for c in changed] == [(tid, "done")]


def test_a_lease_hand_off_keeps_the_remote_snapshot(db, monkeypatch):
    from hermes_cli import kanban_session_activity as ksa
    rows = [{"key": "worker/default/t_remote", "id": "t_remote", "board": "default", "gateway": "worker",
             "session": so_helpers.CHAT, "status": "running", "title": "remote work"}]
    monkeypatch.setattr(ksa, "_remote_cards", lambda ids, statuses, stale_ok: list(rows))
    assert nf.card_change_pass(session_db=so_helpers._opener(db), slugs=[]) == []  # seeds
    rows[0]["status"] = "done"
    _new_process()

    changed = nf.card_change_pass(session_db=so_helpers._opener(db), slugs=[])

    assert [(c["chat_id"], c["source"]) for c in changed] == [(so_helpers.CHAT, "remote")]


# -- producer admission ----------------------------------------------------------------------------------------------


def test_producer_admission_sweeps_only_in_the_lease_holder(monkeypatch):
    swept = []
    monkeypatch.setattr(pa, "sweep_once", lambda: swept.append(1) or [])
    standby, holder = _Lease(False), _Lease(True)

    assert pa.run_pass(standby) is False and swept == [] and standby.beats == 0
    assert pa.run_pass(holder) is True and swept == [1] and holder.beats == 1


# -- roles -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, desktop, role", [("gateway", False, "gateway"), ("serve", False, "serve"),
                                                  ("tui", False, "serve"), ("other", True, "serve"),
                                                  ("other", False, "dashboard"), ("cli", False, "dashboard")])
def test_process_role(monkeypatch, kind, desktop, role):
    from hermes_cli import process_identity
    from hermes_cli.observability import shared_metrics_process as smp
    monkeypatch.setattr(smp, "current_process_kind", lambda: kind)
    monkeypatch.setattr(process_identity, "is_desktop_owned_backend", lambda argv=None: desktop)
    assert hll.process_role() == role
