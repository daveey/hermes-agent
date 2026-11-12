"""Legacy Desktop ``tui`` kanban rows on chats the canonical gateway owns (t_01a1ce31dbbb74bd).

Real SQLite board, real collector/delivery classes; only the session authority (live LOCAL sessions +
ledger) and the FIFO admission (``admit_route_wake``) are faked at their boundaries.
"""
import asyncio
import contextlib
import logging
import os
import sqlite3

import pytest

from gateway import session_automation
from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state_runtime import RuntimeStoreError

CHAT_A, CHAT_B = "20261026_170127_f07c32", "20261026_235722_bef6c6"
_ASK = dict(action="Choose A. Unblock = proceed; archive = drop it.", why_human="Owner decision",
            interaction="reply", scope="Fixture", evidence="Fixture decision")


class _Ledger:
    """The gateway's state.db surface the owner probe reads: ledger rows and turn leases."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT)")
        self.conn.execute("CREATE TABLE worker_executions (session_id TEXT, status TEXT)")
        self.leases = {}

    @contextlib.contextmanager
    def _read_ctx(self):
        yield self.conn

    def work(self, session_id, status="started"):
        self.conn.execute("INSERT INTO session_admissions VALUES (?, ?)", (session_id, status))

    def clear(self):
        self.conn.execute("DELETE FROM session_admissions")

    def session_turn_lease_owner(self, session_id):
        holder = self.leases.get(session_id)
        return {"conversation_id": session_id, "holder": holder} if holder else None


class _Live:
    def __init__(self, session_id):
        self.source = SessionSource(platform=Platform.LOCAL, chat_id=session_id, chat_type="dm")
        self.route = build_session_key(self.source)


class _Authority:
    def __init__(self):
        self.db = _Ledger()
        self.sessions = {}
        self.lineage = {}

    def logical_owner(self, session_id):
        return self.lineage.get(session_id, session_id)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner.adapters = {Platform.LOCAL: object()}
    runner._profile_adapters = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = "default"
    runner.session_authorities = None
    runner.session_authority = authority = _Authority()
    runner.config = None
    runner._session_key_for_source = build_session_key
    admitted = []
    outcome = {"value": True}

    async def fake_admit(runner_, adapter, source, text, producer, metadata=None):
        if isinstance(outcome["value"], Exception):
            raise outcome["value"]
        if outcome["value"]:
            admitted.append({"chat": source.chat_id, "text": text, "producer": producer, "metadata": metadata})
        return outcome["value"]

    monkeypatch.setattr(session_automation, "admit_route_wake", fake_admit)
    return runner, authority, admitted, outcome


def _task(title, chat, *, platform="tui", mode="notify", profile="default", chat_type="dm"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title=title, assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat, chat_type=chat_type,
                           notifier_profile=profile, delivery_mode=mode)
        return tid
    finally:
        conn.close()


def _complete(tid, summary="handoff"):
    conn = kbc.connect()
    try:
        assert kb.complete_task(conn, tid, summary=summary, force=True)
    finally:
        conn.close()


def _block(tid, reason="needs a decision"):
    conn = kbc.connect()
    try:
        assert kb.block_task(conn, tid, reason=reason, kind="needs_input", human_request=_ASK)
    finally:
        conn.close()


def _row(tid, chat, platform="tui"):
    conn = kbc.connect()
    try:
        return conn.execute(
            "SELECT last_event_id, inflight_from, inflight_pid FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ?", (tid, platform, chat)).fetchone()
    finally:
        conn.close()


def _max_event(tid):
    conn = kbc.connect()
    try:
        return conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
    finally:
        conn.close()


async def _tick(runner):
    deliveries = await asyncio.to_thread(_notifier_collect, runner, kb, notifier_profile="default", gc_due=False,
                                         gc_retention_days=30)
    for d in deliveries:
        await _KanbanNotification(runner, d, platform_cls=Platform, sub_fail_counts={}).deliver()
    return deliveries


def _claim_as_legacy(tid, chat, monkeypatch, *, age_s=0):
    """Chat A's legacy Desktop backend (another LIVE pid) claims its row and holds it in flight."""
    with monkeypatch.context() as m:
        m.setattr(kbn, "_my_holder", lambda: (os.getppid(), None))
        _legacy_claim(tid, chat, age_s)


def _legacy_claim(tid, chat, age_s):
    conn = kbc.connect()
    try:
        _o, _n, events = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=chat,
                                                         thread_id="", kinds=("completed", "blocked"),
                                                         durable=True)
        assert events
        if age_s:
            conn.execute("UPDATE kanban_notify_subs SET inflight_at = inflight_at - ? WHERE task_id = ?",
                         (age_s, tid))
            conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_busy_owned_chat_b_gets_completed_and_blocked_within_one_tick_while_chat_a_is_held(env, monkeypatch):
    runner, authority, admitted, _ = env
    a = _task("chat A card", CHAT_A)
    _complete(a)
    _claim_as_legacy(a, CHAT_A, monkeypatch)
    a_before = tuple(_row(a, CHAT_A))

    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B, "started")  # the gateway is mid-turn in chat B
    done, blocked = _task("B done", CHAT_B), _task("B blocked", CHAT_B, chat_type="")
    _complete(done)
    _block(blocked)

    await _tick(runner)

    assert len(admitted) == 2
    assert {x["chat"] for x in admitted} == {CHAT_B} and {x["producer"] for x in admitted} == {"kanban-wake"}
    texts = "\n".join(x["text"] for x in admitted)
    assert f"✔ [default] @worker Kanban {done}" in texts and f"⏸ [default] @worker Kanban {blocked}" in texts
    for tid in (done, blocked):
        last, inflight_from, inflight_pid = _row(tid, CHAT_B)
        assert last == _max_event(tid) and inflight_from is None and inflight_pid is None
    assert tuple(_row(a, CHAT_A)) == a_before  # chat A's live holder is untouched


@pytest.mark.asyncio
async def test_legacy_only_chat_and_idle_owned_chat_stay_with_the_legacy_backend(env):
    runner, authority, admitted, _ = env
    legacy = _task("legacy chat", CHAT_A)
    _complete(legacy)
    authority.sessions[CHAT_B] = _Live(CHAT_B)  # live on the gateway, but no ledger work and no lease
    idle = _task("idle chat", CHAT_B)
    _complete(idle)
    assert await _tick(runner) == []
    assert admitted == []
    assert _row(legacy, CHAT_A)[0] < _max_event(legacy) and _row(idle, CHAT_B)[0] < _max_event(idle)


@pytest.mark.asyncio
async def test_turn_lease_held_by_this_gateway_counts_as_owned(env):
    runner, authority, admitted, _ = env
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.leases[CHAT_B] = f"pid={os.getpid()}:turn={CHAT_B}:x:platform=local"
    tid = _task("leased", CHAT_B)
    _complete(tid)
    await _tick(runner)
    assert [a["chat"] for a in admitted] == [CHAT_B]


@pytest.mark.asyncio
async def test_foreign_lease_or_unknown_admission_is_not_claimed(env):
    runner, authority, admitted, _ = env
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.leases[CHAT_B] = "pid=1:turn=x:platform=desktop"
    authority.db.work(CHAT_B, "unknown")  # never resolved by waiting: nothing may queue behind it
    tid = _task("unknown", CHAT_B)
    _complete(tid)
    assert await _tick(runner) == []
    assert admitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeStoreError("queue_full")])
async def test_refused_admission_rewinds_without_dropping_and_retries(env, failure):
    runner, authority, admitted, outcome = env
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    tid = _task("refused", CHAT_B)
    before = _row(tid, CHAT_B)[0]
    _complete(tid)
    outcome["value"] = failure
    for _ in range(14):  # more than MAX_SEND_FAILURES: a tui row is never dropped for a refusal
        await _tick(runner)
    last, inflight_from, _pid = _row(tid, CHAT_B)
    assert (last, inflight_from) == (before, None)
    outcome["value"] = True
    await _tick(runner)
    assert len(admitted) == 1 and _row(tid, CHAT_B)[0] == _max_event(tid)


@pytest.mark.asyncio
async def test_session_gone_at_delivery_rewinds_and_never_restores(env, monkeypatch):
    runner, authority, admitted, _ = env
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    tid = _task("closing", CHAT_B)
    before = _row(tid, CHAT_B)[0]
    _complete(tid)
    deliveries = await asyncio.to_thread(_notifier_collect, runner, kb, notifier_profile="default", gc_due=False,
                                         gc_retention_days=30)
    assert len(deliveries) == 1
    authority.sessions.clear()  # the chat closed between claim and delivery
    await _KanbanNotification(runner, deliveries[0], platform_cls=Platform, sub_fail_counts={}).deliver()
    assert admitted == [] and tuple(_row(tid, CHAT_B))[:2] == (before, None)


@pytest.mark.asyncio
async def test_compression_tip_chat_id_resolves_to_the_live_lineage_root(env):
    runner, authority, admitted, _ = env
    tip = CHAT_B + "_tip"
    authority.lineage[tip] = CHAT_B
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    tid = _task("compressed", tip)
    _complete(tid)
    await _tick(runner)
    assert [a["chat"] for a in admitted] == [CHAT_B]


@pytest.mark.asyncio
async def test_local_sibling_row_delivers_instead_of_a_second_wake(env):
    runner, authority, _admitted, _ = env
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    tid = _task("both rows", CHAT_B)
    conn = kbc.connect()
    try:
        kbn.add_notify_sub(conn, task_id=tid, platform="local", chat_id=CHAT_B, chat_type="dm",
                           notifier_profile="default", delivery_mode="notify+wake")
    finally:
        conn.close()
    _complete(tid)
    deliveries = await asyncio.to_thread(_notifier_collect, runner, kb, notifier_profile="default", gc_due=False,
                                         gc_retention_days=30)
    assert [d["sub"]["platform"] for d in deliveries] == ["local"]


@pytest.mark.asyncio
async def test_other_served_profile_and_no_local_adapter_are_left_alone(env):
    runner, authority, admitted, _ = env
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    other = _task("other profile", CHAT_B, profile="work")
    _complete(other)
    assert await _tick(runner) == []
    runner.adapters = {Platform.TELEGRAM: object()}
    mine = _task("no local adapter", CHAT_B)
    _complete(mine)
    assert await _tick(runner) == []
    assert admitted == []


@pytest.mark.asyncio
async def test_live_legacy_holder_blocks_until_it_hands_back_then_gateway_claims(env, monkeypatch, caplog):
    runner, authority, admitted, _ = env
    tid = _task("held", CHAT_B)
    _complete(tid)
    _claim_as_legacy(tid, CHAT_B, monkeypatch)
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    assert await _tick(runner) == []  # a fresh live claim is the legacy backend's
    conn = kbc.connect()
    try:  # the legacy backend is refused (SESSION_NOT_OWNED) and hands the range back
        last, inflight_from, _pid = _row(tid, CHAT_B)
        assert kbn.rewind_notify_cursor(conn, task_id=tid, platform="tui", chat_id=CHAT_B, thread_id="",
                                        claimed_cursor=last, old_cursor=inflight_from)
    finally:
        conn.close()
    await _tick(runner)
    assert [x["chat"] for x in admitted] == [CHAT_B]


@pytest.mark.asyncio
async def test_stale_live_claim_is_released_logged_and_then_delivered(env, monkeypatch, caplog):
    runner, authority, admitted, _ = env
    tid = _task("stale", CHAT_B)
    _complete(tid)
    _claim_as_legacy(tid, CHAT_B, monkeypatch, age_s=kbn.NOTIFY_CLAIM_STALE_S + 60)
    authority.sessions[CHAT_B] = _Live(CHAT_B)
    authority.db.work(CHAT_B)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db_notify"):
        await _tick(runner)
    assert any("was never delivered" in r.getMessage() and "without an ack" in r.getMessage()
               for r in caplog.records)
    assert [x["chat"] for x in admitted] == [CHAT_B] and _row(tid, CHAT_B)[0] == _max_event(tid)
