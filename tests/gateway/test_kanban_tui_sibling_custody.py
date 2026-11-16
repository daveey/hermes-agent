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
        self.conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")

    @contextlib.contextmanager
    def _read_ctx(self):
        yield self.conn

    def work(self, session_id, status="started"):
        self.conn.execute("INSERT INTO session_admissions VALUES (?, ?)", (session_id, status))

    def chain(self, *ids):
        """A compaction chain, tip first: each id's parent is the next (``sessions.parent_session_id``)."""
        for child, parent in zip(ids, (*ids[1:], None)):
            self.conn.execute("INSERT INTO sessions VALUES (?, ?)", (child, parent))

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

    from tui_gateway import kanban_leg_custody as custody
    authority.owner_db, authority.owner_opens = authority.db, {}
    real_read = custody._ledger_and_leases

    @contextlib.contextmanager
    def owner_db(session):  # the legacy owner probe reads the same ledger, in one open of the store
        key = session["session_key"]
        authority.owner_opens[key] = authority.owner_opens.get(key, 0) + 1
        yield authority.owner_db

    monkeypatch.setattr(custody, "_ledger_and_leases", lambda session, ids, _db: real_read(session, ids, owner_db))
    monkeypatch.setattr(custody, "_owners", {})
    monkeypatch.setattr(custody, "_live_gateway_pid", lambda: 4242)  # a verified live default gateway
    monkeypatch.setattr(custody, "_gateway_launch_profile", lambda pid: "default")
    return runner, authority, adapter, admitted


def _sub(tid, platform, mode, chat=CHAT, profile="default"):
    conn = kbc.connect()
    try:
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat, chat_type="dm",
                           notifier_profile=profile, delivery_mode=mode)
    finally:
        conn.close()


def _card(*, tui=True, local=True, profile="default"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="FIXTURE", assignee="worker")
    finally:
        conn.close()
    if tui:
        _sub(tid, "tui", "notify", profile=profile)
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


def _next_poll(session):
    """A later 5 s poll: the chat's gateway-owner answer is reused only within ``OWNER_TTL_S``."""
    session.pop("_kb_gateway_owner", None)


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
    _next_poll(session)
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
    _next_poll(session)
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
    _runner, authority, _adapter, _admitted = env
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

    authority.owner_db = _Db()
    monkeypatch.setattr(custody, "_live_gateway_pid", lambda: 31337)
    _next_poll(session)
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))
    assert len(session["_kanban_pending"]) == 1, "a lease held by a non-gateway process is not the gateway's"

    monkeypatch.setattr(custody, "_live_gateway_pid", lambda: 424242)
    _next_poll(session)
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    assert _row(tid)[1] is None


def test_gateway_owned_chat_skips_the_remote_collection_a_legacy_only_chat_keeps_it(env, monkeypatch):
    """Review 5881224107 nit: an owned chat's remote notices were claimed each poll, then released at once by the
    hand-back (``_kb_rewind_refused(not_owned=True)``). The gateway's ``kanban_remote_owner`` (or the sweep)
    delivers them, so the legacy poller no longer claims them at all."""
    _runner, authority, _adapter, _admitted = env
    asked = []
    monkeypatch.setattr(server, "_collect_remote_kanban_notifications",
                        lambda key, claims, *, session=None: asked.append(key) or [])
    session = _session()
    _legacy_collect(session)
    assert asked == [CHAT], "a legacy-only chat still collects its remote notices"

    authority.sessions[CHAT] = _Live(CHAT)
    authority.db.work(CHAT)  # the gateway owns the chat
    _next_poll(session)
    server._notif_poll_kanban_scoped("sid", session)
    assert asked == [CHAT], "an owned chat's poll claims no remote notice"


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


def _keeps_the_chat_and_holds_it_once(runner, authority, adapter, admitted, tid, session):
    """The legacy leg keeps a chat the gateway would not claim: it claims the notice once, never hands it back,
    and nobody else delivers it, before and after the gateway's ledger work ends (review 5887848770 nit 1)."""
    assert len(_legacy_collect(session)) == 1 and _row(tid)[1] is not None
    for _ in range(2):
        server._notif_poll_kanban_scoped("sid", session, prepared=([], []))  # one legacy poll, still busy
        assert len(session["_kanban_pending"]) == 1 and _row(tid)[1] is not None, "handed to a leg that never claims"
        _gateway_tick(runner)
        assert admitted == [] and adapter.turns == []
        _next_poll(session)
    authority.db.conn.execute("UPDATE session_admissions SET status = 'terminal'")  # the gateway's work ends
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))
    assert _legacy_collect(session) == [] and len(session["_kanban_pending"]) == 1
    _gateway_tick(runner)
    assert _notices(adapter, admitted, tid) == 0 and f"Kanban {tid}" in session["_kanban_pending"][0]


def test_nit1_a_forwardable_ledger_on_a_profile_the_gateway_does_not_serve_keeps_the_chat(env):
    """(a) The gateway holds forwardable ledger work for the chat, but the row is stamped for a profile its
    collector does not serve (``_launch_authority`` answers None), so only this backend can deliver it."""
    runner, authority, adapter, admitted = env
    tid = _card(local=False, profile="coder")
    authority.sessions[CHAT] = _Live(CHAT)
    authority.db.work(CHAT)
    session = _session()
    session["running"] = True
    _complete(tid)
    _keeps_the_chat_and_holds_it_once(runner, authority, adapter, admitted, tid, session)


def test_nit1_forwardable_ledger_work_with_no_local_adapter_keeps_the_chat(env, monkeypatch):
    """(b) The ledger still holds forwardable work, but no live gateway runs a LOCAL adapter for the chat (none is
    running: the owner probe finds no verified default gateway), so nothing would claim a handed-back row."""
    from tui_gateway import kanban_leg_custody as custody
    runner, authority, adapter, admitted = env
    runner.adapters = {}  # and the collector, were it running, has no LOCAL adapter either
    monkeypatch.setattr(custody, "_live_gateway_pid", lambda: None)
    tid = _card(local=False)
    authority.db.work(CHAT)
    session = _session()
    session["running"] = True
    _complete(tid)
    _keeps_the_chat_and_holds_it_once(runner, authority, adapter, admitted, tid, session)


def test_nit1_the_hand_back_starts_an_info_episode_once_per_chat(env, monkeypatch, caplog):
    from tui_gateway import kanban_leg_custody as custody
    _runner, authority, _adapter, _admitted = env
    clock = {"t": 1000.0}
    monkeypatch.setattr(custody, "time", type("T", (), {"monotonic": staticmethod(lambda: clock["t"])}))
    authority.db.work(CHAT)
    session = _session()
    with caplog.at_level("INFO", logger=custody.__name__):
        for _ in range(4):
            assert server._kb_gateway_owns(session)
            clock["t"] += custody.OWNER_TTL_S
        authority.db.conn.execute("UPDATE session_admissions SET status = 'terminal'")
        assert not server._kb_gateway_owns(session)
    lines = [r.getMessage() for r in caplog.records if r.name == custody.__name__]
    assert len(lines) == 2 and "goes to gateway pid 4242" in lines[0] and "back with this backend" in lines[1]


def test_nit2_the_owner_probe_opens_state_db_at_most_once_per_chat_per_ttl(env, monkeypatch):
    """Every live chat's legacy poller asks whether the gateway owns it on each 5 s poll, and again before running
    a buffered batch. Over N polls of M chats with compaction parents, state.db is opened at most once per chat per
    ``OWNER_TTL_S`` (review 5887848770 nit 2; the per-poll cost t_01a1cee98b798343 removed from the lineage read)."""
    from tui_gateway import kanban_leg_custody as custody
    _runner, authority, _adapter, _admitted = env
    assert custody.OWNER_TTL_S <= server._KANBAN_POLL_SECONDS
    clock = {"t": 1000.0}
    monkeypatch.setattr(custody, "time", type("T", (), {"monotonic": staticmethod(lambda: clock["t"])}))
    chats = [f"chat-{i}" for i in range(3)]
    monkeypatch.setattr(server, "_session_lineage", lambda key, limit=8: (f"{key}-root", f"{key}-older"))
    authority.db.work("chat-0-root")  # one chat is gateway-owned through its lineage root; the others are not
    opens = authority.owner_opens  # each open of the session's store by the owner probe
    sessions = {c: {"session_key": c, "history_lock": threading.Lock()} for c in chats}
    polls, step = 24, 1.25  # 30 s of polling, each poll asking twice
    for _ in range(polls):
        for session in sessions.values():
            _legacy_collect(session)
            server._kb_gateway_owns(session)  # the buffered batch's hand-back check
        clock["t"] += step
    budget = int(polls * step / custody.OWNER_TTL_S)
    assert set(opens) == set(chats) and all(n <= budget for n in opens.values()), (opens, budget)
    assert server._kb_gateway_owns(sessions["chat-0"]) and not server._kb_gateway_owns(sessions["chat-1"])


def _clock(monkeypatch):
    from tui_gateway import kanban_leg_custody as custody
    clock = {"t": 1000.0}
    monkeypatch.setattr(custody, "time", type("T", (), {"monotonic": staticmethod(lambda: clock["t"])}))
    return clock


def _remote_collected(runner, authority, monkeypatch, tmp_path, logical):
    """The chats the remote-owner pass (``kanban_remote_owner._collect``) collects notices for, with ``logical``'s
    live session as this gateway's only owned chat: it gates each chat on ``_gateway_holds_work``."""
    from gateway import kanban_remote_owner as kro
    from tui_gateway import kanban_remote_notices as krn
    hub = tmp_path / "hub.db"
    hub.touch()
    monkeypatch.setattr(krn, "hub_db_path", lambda: hub)
    monkeypatch.setattr(kro, "_reconcile", lambda db: None)
    collected = []
    monkeypatch.setattr(krn, "collect", lambda chat, profile, **kw: collected.append(chat) or [])
    assert kro._collect(runner, authority, [(logical, authority.sessions[logical])]) == []
    return collected


@pytest.mark.parametrize("tip_status,root_status", [("queued", "unknown"), ("unknown", "queued")],
                         ids=["tip_queued_root_unknown", "tip_unknown_root_queued"])
def test_nit1_one_unknown_on_tip_or_root_answers_one_rule_on_both_legs(env, monkeypatch, tmp_path, tip_status,
                                                                         root_status):
    """One id holds ``queued`` work and the other an ``unknown`` admission, in either layout. Key-first the legacy
    leg read the tip and sorted the gateway read the older root, so one layout handed the chat back to a gateway
    that never claimed (review 5888998970 nit 1); the swapped layout pins the rule in the other direction (review
    5889497938 nit 2). One rule over the ids: neither owns, so the legacy leg keeps the chat, and the remote-owner
    pass collects nothing for it either."""
    from gateway.kanban_tui_owner import _gateway_holds_work, owned_live_session
    from tui_gateway import kanban_leg_custody as custody
    runner, authority, adapter, admitted = env
    root = "20261020_101500_a1b2c3"  # older than the tip: first sorted, second key-first
    monkeypatch.setattr(server, "_session_lineage", lambda key, limit=8: (root,) if key == CHAT else ())
    monkeypatch.setattr(authority, "logical_owner", lambda chat: root)
    authority.sessions[root] = _Live(CHAT)
    authority.db.work(CHAT, tip_status)
    authority.db.work(root, root_status)
    with authority.db._read_ctx() as conn:
        answers = {custody.lineage_ledger_status(conn, ids) for ids in ((CHAT, root), (root, CHAT), {CHAT, root})}
    assert answers == {"unknown"}
    assert not _gateway_holds_work(authority, {CHAT, root})
    assert not server._kb_gateway_owns(_session())
    assert _remote_collected(runner, authority, monkeypatch, tmp_path, root) == []
    tid = _card(local=False)
    assert owned_live_session(runner, {"chat_id": CHAT, "notifier_profile": "default", "task_id": tid}) is None
    session = _session()
    session["running"] = True
    _complete(tid)
    _keeps_the_chat_and_holds_it_once(runner, authority, adapter, admitted, tid, session)


def test_remote_owner_collects_for_a_chat_both_legs_hand_to_the_gateway(env, monkeypatch, tmp_path):
    """The remote-owner pass's control: forwardable work on the root alone, so both legs agree the gateway owns
    the chat, and ``_collect`` collects its remote notices."""
    from gateway.kanban_tui_owner import _gateway_holds_work
    runner, authority, _adapter, _admitted = env
    root = "20261020_101500_a1b2c3"
    monkeypatch.setattr(server, "_session_lineage", lambda key, limit=8: (root,) if key == CHAT else ())
    monkeypatch.setattr(authority, "logical_owner", lambda chat: root)
    authority.sessions[root] = _Live(CHAT)
    authority.db.work(root, "queued")
    assert _gateway_holds_work(authority, {CHAT, root}) and server._kb_gateway_owns(_session())
    assert _remote_collected(runner, authority, monkeypatch, tmp_path, root) == [CHAT]


@pytest.mark.parametrize("first", ["queued", "unknown"])
def test_an_unknown_beside_a_queued_row_on_one_id_is_never_masked(env, first):
    """One id with a ``queued`` and an ``unknown`` row, in either insertion order: the per-id read is not one
    arbitrary row (``_CANONICAL_PENDING_SQL``'s ``LIMIT 1``), so the ``unknown`` admission always answers and
    neither leg owns (review 5889497938 nit 1)."""
    from gateway.kanban_tui_owner import _gateway_holds_work
    from tui_gateway import kanban_leg_custody as custody
    _runner, authority, _adapter, _admitted = env
    for status in (first, ({"queued", "unknown"} - {first}).pop()):
        authority.db.work(CHAT, status)
    with authority.db._read_ctx() as conn:
        assert custody.lineage_ledger_status(conn, (CHAT,)) == "unknown"
    assert not _gateway_holds_work(authority, {CHAT})
    assert not server._kb_gateway_owns(_session())


def _three_deep(monkeypatch, authority):
    """Tip CHAT, an intermediate compaction parent, and the lineage root the authority keys the live session by."""
    mid, root = "20261023_090000_d4e5f6", "20261020_101500_a1b2c3"
    authority.db.chain(CHAT, mid, root)
    monkeypatch.setattr(server, "_session_lineage", lambda key, limit=8: (mid, root) if key == CHAT else ())
    monkeypatch.setattr(authority, "logical_owner", lambda chat: root)
    authority.sessions[root] = _Live(CHAT)
    return mid, root


def test_nit3_an_unknown_on_an_intermediate_parent_keeps_the_chat_on_both_legs(env, monkeypatch, tmp_path):
    """An ``unknown`` admission on a compaction parent between the tip and the root, ``queued`` work on the root.
    The gateway read only {tip, root} and claimed while the legacy leg (which reads every parent) kept and claimed
    the chat too (review 5889497938 nit 3). The gateway now reads the same parents: neither owns, the legacy leg
    delivers the notice once, and the remote-owner pass stands aside."""
    from gateway.kanban_tui_owner import _gateway_holds_work, owned_live_session
    runner, authority, adapter, admitted = env
    mid, root = _three_deep(monkeypatch, authority)
    authority.db.work(mid, "unknown")
    authority.db.work(root, "queued")
    assert not server._kb_gateway_owns(_session())
    assert not _gateway_holds_work(authority, {CHAT, root})
    assert _remote_collected(runner, authority, monkeypatch, tmp_path, root) == []
    tid = _card(local=False)
    assert owned_live_session(runner, {"chat_id": CHAT, "notifier_profile": "default", "task_id": tid}) is None
    session = _session()
    session["running"] = True
    _complete(tid)
    _keeps_the_chat_and_holds_it_once(runner, authority, adapter, admitted, tid, session)


def test_nit3_forwardable_work_on_an_intermediate_parent_alone_is_claimed_by_the_gateway_once(env, monkeypatch):
    """The other direction: ``queued`` work only on the intermediate parent. The legacy leg handed the chat back
    and the gateway, reading {tip, root}, claimed nothing until that work ended. Both legs now read it: the hand-back
    reaches a gateway that admits the notice within one tick, exactly once."""
    runner, authority, adapter, admitted = env
    mid, _root = _three_deep(monkeypatch, authority)
    authority.db.work(mid, "queued")
    tid = _card(local=False)
    session = _session()
    session["running"] = True
    _complete(tid)
    assert server._kb_gateway_owns(session)
    assert _legacy_collect(session) == []  # handed to the gateway
    _gateway_tick(runner)
    assert [a["chat"] for a in admitted] == [CHAT]
    _gateway_tick(runner)
    _next_poll(session)
    assert _legacy_collect(session) == []
    assert _notices(adapter, admitted, tid) == 1


def test_the_gateway_walks_as_many_parents_as_the_legacy_lineage(env):
    from tui_gateway import kanban_leg_custody as custody
    _runner, authority, _adapter, _admitted = env
    authority.db.chain(*(f"s{i}" for i in range(custody.LINEAGE_LIMIT + 3)))
    with authority.db._read_ctx() as conn:
        assert custody.with_parents(conn, {"s0"}) == tuple(f"s{i}" for i in range(custody.LINEAGE_LIMIT + 1))
        assert custody.with_parents(conn, {"unknown-id"}) == ("unknown-id",)
        conn.execute("ALTER TABLE sessions RENAME TO sessions_gone")
        assert custody.with_parents(conn, ("s0", "s1")) == ("s0", "s1")  # a read error keeps the ids given


def test_restore_gap_unrestored_gateway_chat_is_back_within_one_ttl_once_its_work_ends(env, monkeypatch):
    """The gap ``gateway_owner`` names (review 5888998970 nit 2): the gateway holds forwardable ledger work for the
    chat but has not restored it as a live LOCAL session (just restarted), so nobody claims the tui row while the
    work runs. It is bounded: once the work ends, the legacy leg takes the chat back within one ``OWNER_TTL_S`` and
    claims the notice exactly once."""
    from tui_gateway import kanban_leg_custody as custody
    runner, authority, adapter, admitted = env
    clock = _clock(monkeypatch)
    tid = _card(local=False)
    authority.db.work(CHAT)  # ledger work, but no live session in authority.sessions
    session = _session()
    session["running"] = True
    _complete(tid)
    for _ in range(3):
        assert _legacy_collect(session) == []  # handed to the gateway, which claims nothing
        _gateway_tick(runner)
        assert admitted == [] and adapter.turns == [] and _row(tid)[1] is None
        clock["t"] += custody.OWNER_TTL_S / 2
    authority.db.conn.execute("UPDATE session_admissions SET status = 'terminal'")  # the gateway's work ends
    ended = clock["t"]
    while not (texts := _legacy_collect(session)):
        assert clock["t"] - ended <= custody.OWNER_TTL_S, "the legacy leg never took the chat back"
        clock["t"] += 0.5
    assert len(texts) == 1 and f"Kanban {tid}" in texts[0] and _row(tid)[1] is not None
    for _ in range(2):
        _gateway_tick(runner)
        clock["t"] += custody.OWNER_TTL_S
        assert _legacy_collect(session) == []
    assert _notices(adapter, admitted, tid) == 0 and len(session["_kanban_pending"]) == 1


def test_gateway_launch_profile_reads_a_real_gateway_state_json(tmp_path, monkeypatch):
    """``_gateway_launch_profile`` unstubbed (review 5888998970 nit 3): a real ``gateway_state.json`` under a temp
    Hermes root, read back through ``gateway.status.read_runtime_status``."""
    import json

    from hermes_constants import get_default_hermes_root
    from tui_gateway import kanban_leg_custody as custody
    home = tmp_path / "hermes-root"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert get_default_hermes_root() == home
    state = home / "gateway_state.json"
    assert custody._gateway_launch_profile(4242) == ""  # no record yet

    def record(**fields):
        state.write_text(json.dumps({"pid": 4242, "gateway_state": "running", **fields}), encoding="utf-8")

    record(served_profiles=["coder", "default"])
    assert custody._gateway_launch_profile(4242) == "coder"  # served_profiles[0] is the launch profile
    assert custody._gateway_launch_profile(31337) == ""  # the record names another gateway pid
    record()
    assert custody._gateway_launch_profile(4242) == ""  # no served_profiles
    record(served_profiles=[])
    assert custody._gateway_launch_profile(4242) == ""  # empty served_profiles


def test_busy_non_owner_backend_hands_a_remote_notice_back_when_the_gateway_owns_the_chat(env, tmp_path,
                                                                                          monkeypatch):
    """Remote (hub-replica) notices follow the same hand-back as tui rows (t_01a1d46be379dfd3).

    Chat 20261026_235722_bef6c6 on David-MBP, 2026-11-13 (PST), core 2cc7101849: worker-2's t_01a1d405586c5ba1 was
    observed at 00:34:33 but reached the chat only at 02:08:50. The chat was gateway-owned (marker writer pid
    81192) and also loaded by Desktop backends; a busy backend's legacy poller claimed the remote notice into
    its buffer and held it in flight until its own turn ended (02:07:45 "rewound 1 of 1"), so the owner's sweep
    found it claimed elsewhere and skipped it silently. A busy non-owner must release the ledger claim on its
    next poll, so the owner's next pass offers it."""
    import os
    import time

    from tests.tui_gateway.test_kanban_remote_notices import _card as remote_card, _hub, _ledger_row
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [remote_card("t_remote_custody", CHAT, status="done", at=now - 4)])
    ledger = tmp_path / "remote-notices.db"
    krn._reset_for_tests()
    monkeypatch.setattr(krn, "hub_db_path", lambda: replica)
    monkeypatch.setattr(krn, "ledger_path", lambda: ledger)
    ref = "hub/default/default/t_remote_custody"

    session = _session()
    session["running"] = True  # this backend is mid-turn: the poll buffers what it claims
    _legacy_collect(session)
    assert [c["remote"]["ref"] for c in session["_kanban_claims"]] == [ref]
    assert _ledger_row(ledger, CHAT, ref)["holder_pid"] == os.getpid()  # in flight under this backend

    authority = env[1]
    authority.sessions[CHAT] = _Live(CHAT)
    authority.db.work(CHAT)  # the gateway owns the chat
    _next_poll(session)
    server._notif_poll_kanban_scoped("sid", session, prepared=([], []))  # one poll, still mid-turn
    assert session["_kanban_pending"] == [] and session["_kanban_claims"] == []
    assert _ledger_row(ledger, CHAT, ref) is None, "the remote claim waited in flight for this backend's turn"

    offered = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)  # the owner's next pass
    assert [item["ref"] for item in offered] == [ref]
