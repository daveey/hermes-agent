"""A chat's ``tui`` row and its waking ``local`` row deliver each Kanban event once, and a gateway-owned busy chat
gets its tui notices within one notifier tick (t_01a1cfbe6c8d46e7). [daveey]

Chat 20261026_235722_bef6c6 on the MacBook, 2026-11-12 (PST):

- t_01a1cfb6db097701 completed at 04:25:04. The local wake delivered it (04:40:25, row 879522) while the legacy
  backend held a tui claim on it; the claim was reaped, re-claimed, rewound, then the gateway admitted the tui
  notice too and it ran as a second turn at 04:59:20 (row 880235).
- t_01a1ce412fbf6e7f completed at 04:24:52; the chat's local row was created at 04:39:43 with its cursor already
  past the completion. Its only leg was tui, held in flight by the busy chat's legacy backend for 15 min, then by
  another backend: 45 min without delivery.

Real SQLite board, real gateway collector/delivery and legacy poller; only the session authority, the ledger
probe and the FIFO admission are faked at their boundaries.
"""
import asyncio
import contextlib
import sqlite3
import threading

import pytest

from gateway import kanban_wake_sibling as sibling
from gateway import session_automation
from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server

CHAT = "20261026_235722_bef6c6"


class _Ledger:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE session_admissions (target_session_id TEXT, status TEXT)")
        self.conn.execute("CREATE TABLE worker_executions (session_id TEXT, status TEXT)")

    @contextlib.contextmanager
    def _read_ctx(self):
        yield self.conn

    def work(self, session_id, status="started"):
        self.conn.execute("INSERT INTO session_admissions VALUES (?, ?)", (session_id, status))

    def status(self, session_id):
        row = self.conn.execute("SELECT status FROM session_admissions WHERE target_session_id = ?",
                                (session_id,)).fetchone()
        return row[0] if row else None

    def session_turn_lease_owner(self, session_id):
        return None


class _Live:
    def __init__(self, session_id):
        self.source = SessionSource(platform=Platform.LOCAL, chat_id=session_id, chat_type="dm")
        self.route = build_session_key(self.source)


class _Authority:
    def __init__(self):
        self.db = _Ledger()
        self.sessions = {}

    def logical_owner(self, session_id):
        return session_id


class _LocalAdapter:
    """The LOCAL adapter: ``send`` is a no-op ack, a wake (``handle_message``) is a turn."""

    def __init__(self):
        self.turns = []

    async def send(self, chat_id, text, metadata=None):
        pass

    async def handle_message(self, event):
        self.turns.append(event.text)
        event._gateway_accepted = True


class _Refusal:
    def __init__(self, status):
        self.ledger_status = status


@pytest.fixture
def env(tmp_path, monkeypatch):
    kb.init_db()
    adapter = _LocalAdapter()
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._running_agents = {}
    runner.adapters = {Platform.LOCAL: adapter}
    runner._profile_adapters = {}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = "default"
    runner.session_authorities = None
    runner.session_authority = authority = _Authority()
    runner.config = None
    runner._session_key_for_source = build_session_key
    admitted = []

    async def fake_admit(runner_, adapter_, source, text, producer, metadata=None):
        admitted.append({"chat": source.chat_id, "text": text, "producer": producer})
        return True

    monkeypatch.setattr(session_automation, "admit_route_wake", fake_admit)

    def ledger_refusal(session):  # the legacy backend reads the same ledger the gateway's owner probe reads
        status = authority.db.status(session.get("session_key"))
        return _Refusal(status) if status else None

    monkeypatch.setattr(server, "_canonical_ledger_refusal", ledger_refusal)
    return runner, authority, adapter, admitted


def _sub(tid, platform, mode, chat=CHAT):
    conn = kbc.connect()
    try:
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat, chat_type="dm",
                           notifier_profile="default", delivery_mode=mode)
    finally:
        conn.close()


def _card(*, tui=True, local=True):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="FIXTURE", assignee="worker")
    finally:
        conn.close()
    if tui:
        _sub(tid, "tui", "notify")
    if local:
        _sub(tid, "local", "notify+wake")
    return tid


def _complete(tid):
    conn = kbc.connect()
    try:
        assert kb.complete_task(conn, tid, summary="fixture handoff", force=True)
        return conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = 'completed'",
                            (tid,)).fetchone()[0]
    finally:
        conn.close()


def _archive(tid):
    conn = kbc.connect()
    try:
        assert kb.archive_task(conn, tid)
    finally:
        conn.close()


def _row(tid, platform="tui"):
    conn = kbc.connect()
    try:
        return conn.execute("SELECT last_event_id, inflight_from, inflight_pid FROM kanban_notify_subs "
                            "WHERE task_id = ? AND platform = ? AND chat_id = ?", (tid, platform, CHAT)).fetchone()
    finally:
        conn.close()


def _gateway_tick(runner):
    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb, notifier_profile="default",
                                             gc_due=False, gc_retention_days=30)
        for d in deliveries:
            await _KanbanNotification(runner, d, platform_cls=Platform, sub_fail_counts={}).deliver()
    asyncio.run(tick())


def _session():
    return {"session_key": CHAT, "history_lock": threading.Lock()}


def _legacy_collect(session):
    """The legacy poller's collection into the session buffer (what a busy chat holds until its turn)."""
    claims = []
    texts = server._collect_kanban_notifications(session, claims)
    session.setdefault("_kanban_pending", []).extend(texts)
    session.setdefault("_kanban_claims", []).extend(claims)
    return texts


def _notices(adapter, admitted, tid):
    return sum(f"Kanban {tid}" in t for t in adapter.turns) + sum(f"Kanban {tid}" in a["text"] for a in admitted)


def test_n1_local_sibling_older_than_the_event_delivers_it_and_the_legacy_poller_never_does(env):
    runner, _authority, adapter, admitted = env
    tid = _card()
    _complete(tid)
    session = _session()
    assert _legacy_collect(session) == []  # the wake still owes it: the tui row stays unclaimed
    assert _row(tid)[1] is None
    _gateway_tick(runner)  # the wake delivers and settles the tui row
    assert _legacy_collect(session) == []
    _archive(tid)
    _gateway_tick(runner)
    assert _legacy_collect(session) == [] and session["_kanban_claims"] == []
    assert _notices(adapter, admitted, tid) == 1


def test_n1_a_wake_dropped_undelivered_leaves_the_event_to_the_tui_leg_exactly_once(env):
    _runner, _authority, _adapter, _admitted = env
    tid = _card()
    _complete(tid)
    conn = kbc.connect()
    try:  # _kanban_drop_failed: the local row goes after repeated wake failures, its event undelivered
        kbn.remove_notify_sub(conn, task_id=tid, platform="local", chat_id=CHAT, thread_id="")
    finally:
        conn.close()
    session = _session()
    texts = _legacy_collect(session)
    assert len(texts) == 1 and f"Kanban {tid}" in texts[0]
    assert _legacy_collect(session) == []  # in flight with this holder: not handed out twice


def test_n2_event_before_the_local_sibling_existed_reaches_a_busy_owned_chat_within_one_tick(env):
    runner, authority, adapter, admitted = env
    tid = _card(local=False)
    completed = _complete(tid)
    _sub(tid, "local", "notify+wake")  # created later, its cursor already past the completion
    assert _row(tid, "local")[0] >= completed
    authority.sessions[CHAT] = _Live(CHAT)
    authority.db.work(CHAT)  # the gateway is mid-turn in the chat

    session = _session()
    assert _legacy_collect(session) == []  # the gateway owns the chat: the legacy backend does not claim
    _gateway_tick(runner)  # one notifier tick
    assert [a["chat"] for a in admitted] == [CHAT] and f"Kanban {tid}" in admitted[0]["text"]
    last, inflight_from, _pid = _row(tid)
    assert last >= completed and inflight_from is None
    _gateway_tick(runner)
    assert _legacy_collect(session) == []
    assert _notices(adapter, admitted, tid) == 1


def test_n2_legacy_only_chat_still_delivers_an_event_older_than_the_local_sibling(env):
    runner, _authority, adapter, admitted = env
    tid = _card(local=False)
    _complete(tid)
    _sub(tid, "local", "notify+wake")
    session = _session()
    texts = _legacy_collect(session)
    assert len(texts) == 1 and f"Kanban {tid}" in texts[0]  # silencing on the late cursor lost it
    _gateway_tick(runner)
    assert adapter.turns == [] and admitted == []


def test_n3_a_held_claim_the_wake_delivered_resolves_as_delivered_even_after_archive(env, monkeypatch):
    runner, _authority, adapter, admitted = env
    tid = _card()
    completed = _complete(tid)
    session = _session()
    with monkeypatch.context() as m:  # the wake is stuck past its defer window: the busy chat's backend claims
        m.setattr(sibling, "DEFER_MAX_S", -1.0)
        texts = _legacy_collect(session)
    assert len(texts) == 1 and _row(tid)[1] is not None  # held in flight, buffered behind the busy turn
    _gateway_tick(runner)  # the wake lands late and delivers the completion
    assert sum(f"Kanban {tid}" in t for t in adapter.turns) == 1

    server._kb_settle_held(session)
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    last, inflight_from, inflight_pid = _row(tid)
    assert last >= completed and inflight_from is None and inflight_pid is None  # advanced and cleared

    _archive(tid)
    for _ in range(2):
        _gateway_tick(runner)
        assert _legacy_collect(session) == []
    assert _notices(adapter, admitted, tid) == 1


def test_n3_a_held_claim_on_an_undelivered_event_is_kept(env, monkeypatch):
    tid = _card()
    _complete(tid)
    session = _session()
    with monkeypatch.context() as m:
        m.setattr(sibling, "DEFER_MAX_S", -1.0)
        _legacy_collect(session)
    server._kb_settle_held(session)  # the wake has not delivered: the buffered notice stays owed
    assert len(session["_kanban_pending"]) == 1 and len(session["_kanban_claims"]) == 1


def test_n4_busy_owned_tui_only_chat_never_holds_a_legacy_claim(env):
    runner, authority, adapter, admitted = env
    tid = _card(local=False)
    session = _session()
    session["running"] = True  # the chat is mid-turn
    _complete(tid)
    _legacy_collect(session)  # the chat was legacy-run: claimed and buffered behind the busy turn
    assert len(session["_kanban_pending"]) == 1 and _row(tid)[1] is not None

    authority.sessions[CHAT] = _Live(CHAT)
    authority.db.work(CHAT)  # the gateway takes the chat and is mid-turn
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))  # one legacy poll, still busy
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    assert _row(tid)[1] is None  # handed back, not held until the turn ends or the reaper

    _gateway_tick(runner)  # the next notifier tick admits it to the chat's FIFO
    assert [a["chat"] for a in admitted] == [CHAT]
    assert _row(tid)[1] is None
    _gateway_tick(runner)
    assert _legacy_collect(session) == []
    assert _notices(adapter, admitted, tid) == 1


def test_n4_compressed_owned_chat_with_ledger_work_on_the_root_never_holds_a_legacy_claim(env, monkeypatch):
    """N4 on a compressed chat: the tui row sits on the tip, the gateway's admission on the lineage root (review
    t_01a1d1543b9d13cf, blocking finding of review 5881224107)."""
    runner, authority, adapter, admitted = env
    tid = _card(local=False)
    session = _session()
    session["running"] = True
    _complete(tid)
    _legacy_collect(session)
    assert len(session["_kanban_pending"]) == 1 and _row(tid)[1] is not None

    monkeypatch.setattr(server, "_session_lineage", lambda key, limit=8: ("root",) if key == CHAT else ())
    monkeypatch.setattr(authority, "logical_owner", lambda chat: "root")
    authority.sessions["root"] = _Live(CHAT)
    authority.db.work("root")  # the gateway's ledger keys the chat by its lineage root
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    assert _row(tid)[1] is None

    _gateway_tick(runner)
    assert [a["chat"] for a in admitted] == [CHAT]
    assert _legacy_collect(session) == []
    assert _notices(adapter, admitted, tid) == 1


def test_n4_a_gateway_turn_lease_hands_the_chat_back_like_ledger_work(env, monkeypatch):
    """The gateway owner also claims while its process holds the chat's turn lease (``_gateway_holds_work``); the
    legacy poller must hand back on the same condition, and only for the live gateway's pid."""
    from tui_gateway import kanban_leg_custody as custody
    tid = _card(local=False)
    session = _session()
    session["running"] = True
    _complete(tid)
    _legacy_collect(session)
    assert len(session["_kanban_pending"]) == 1

    holder = {"value": "pid=424242:gateway-turn"}

    class _Db:
        def session_turn_lease_owner(self, sid):
            return {"holder": holder["value"]} if sid == CHAT else None

    @contextlib.contextmanager
    def fake_db(_session):
        yield _Db()

    monkeypatch.setattr(server, "_session_db", fake_db)
    monkeypatch.setattr(custody, "_live_gateway_pid", lambda: 31337)
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))
    assert len(session["_kanban_pending"]) == 1, "a lease held by a non-gateway process is not the gateway's"

    monkeypatch.setattr(custody, "_live_gateway_pid", lambda: 424242)
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    assert _row(tid)[1] is None


def test_legacy_only_idle_chat_still_delivers_once(env):
    runner, _authority, adapter, admitted = env
    tid = _card(local=False)
    _complete(tid)
    session = _session()
    assert len(_legacy_collect(session)) == 1
    assert _legacy_collect(session) == []  # in flight with this holder: never handed out twice
    _gateway_tick(runner)
    assert admitted == [] and adapter.turns == []
    assert len(session["_kanban_pending"]) == 1


def test_local_sibling_on_the_lineage_root_counts_for_a_tui_row_on_the_tip(env):
    tid = _card(tui=False, local=False)
    _sub(tid, "local", "notify+wake", chat="root")
    _sub(tid, "tui", "notify", chat="tip")
    _complete(tid)
    conn = kbc.connect()
    try:
        tui = {"task_id": tid, "platform": "tui", "chat_id": "tip", "thread_id": ""}
        assert not sibling.tui_defer(conn, tui)
        assert sibling.tui_defer(conn, {**tui, "sibling_chats": ("tip", "root")})
    finally:
        conn.close()


def test_notify_sub_records_the_event_it_was_created_at(env):
    tid = _card(tui=False, local=False)
    completed = _complete(tid)
    _sub(tid, "local", "notify+wake")
    conn = kbc.connect()
    try:
        row = conn.execute("SELECT created_event_id, last_event_id FROM kanban_notify_subs WHERE task_id = ?",
                           (tid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == row[1] and row[0] >= completed


def test_a_settled_claim_recovered_before_the_buffer_check_is_still_resolved_after_archive(env, monkeypatch):
    """N3 with recovery first: the wake settles the buffered claim through its end, the card is archived, the next
    tick clears the empty claim and drains the local row, and only then does the busy holder check custody
    (review t_01a1d154902ba0a7)."""
    runner, _, adapter, _ = env
    tid = _card()
    _complete(tid)
    session = _session()
    with monkeypatch.context() as m:
        m.setattr(sibling, "DEFER_MAX_S", -1.0)
        _legacy_collect(session)
    _gateway_tick(runner)
    assert len(adapter.turns) == 1
    _archive(tid)
    _gateway_tick(runner)  # drain and ack the archived local row
    assert _row(tid, "local") is None
    server._kb_settle_held(session)
    assert session["_kanban_pending"] == [], "already delivered buffered notice survived reaper + archive"
    assert session.get("_kanban_claims") in (None, []), "the resolved claim is re-checked forever"
    _gateway_tick(runner)
    assert len(adapter.turns) == 1


def test_a_root_local_wake_settles_the_tip_tui_leg_across_archive(env, monkeypatch):
    runner, authority, adapter, admitted = env
    tid = _card(tui=False, local=False)
    _sub(tid, "local", "notify+wake", chat="root")
    _sub(tid, "tui", "notify", chat=CHAT)
    completed = _complete(tid)
    monkeypatch.setattr(authority, "logical_owner", lambda chat: "root")
    authority.sessions[CHAT] = _Live(CHAT)
    authority.db.work(CHAT)
    _gateway_tick(runner)
    assert len(adapter.turns) == 1
    assert _row(tid)[0] >= completed, "the root wake settles the tip's tui row, not only claim-time cover"
    _archive(tid)
    _gateway_tick(runner)  # drain and ack the archived local row
    _gateway_tick(runner)
    assert admitted == [], "tip tui leg delivered after root local leg and archive"


def test_n3_root_local_wake_resolves_a_held_tip_claim_after_archive(env, monkeypatch):
    """N3 across compression: the wake row is on the lineage root, the held tui claim on the tip; the card is archived
    and the root local row drains before the busy holder re-checks its buffer (review t_01a1d39942db8c2c)."""
    runner, authority, adapter, _admitted = env
    tid = _card(tui=False, local=False)
    _sub(tid, "local", "notify+wake", chat="root")
    _sub(tid, "tui", "notify", chat=CHAT)
    _complete(tid)
    monkeypatch.setattr(server, "_session_lineage", lambda key, limit=8: ("root",) if key == CHAT else ())
    monkeypatch.setattr(authority, "logical_owner", lambda chat: "root")
    session = _session()
    with monkeypatch.context() as m:
        m.setattr(sibling, "DEFER_MAX_S", -1.0)
        assert len(_legacy_collect(session)) == 1 and _row(tid)[1] is not None
    _gateway_tick(runner)  # the root wake delivers the completion
    _archive(tid)
    _gateway_tick(runner)
    _gateway_tick(runner)  # the archived root local row drains and is removed
    server._kb_settle_held(session)
    assert len(adapter.turns) + len(session["_kanban_pending"]) == 1
    assert session.get("_kanban_claims") in (None, [])
    _gateway_tick(runner)
    assert _legacy_collect(session) == [] and len(adapter.turns) == 1


def test_a_root_wake_floor_reads_the_tip_tui_row(env):
    tid = _card(tui=False, local=False)
    _sub(tid, "local", "notify+wake", chat="root")
    _sub(tid, "tui", "notify", chat=CHAT)
    conn = kbc.connect()
    try:
        local = {"task_id": tid, "platform": "local", "chat_id": "root", "thread_id": "", "delivery_mode": "notify+wake"}
        assert sibling.tui_sibling_floor(conn, local) is None
        assert sibling.tui_sibling_floor(conn, {**local, "tui_chats": (CHAT,)}) == _row(tid)[0]
    finally:
        conn.close()
