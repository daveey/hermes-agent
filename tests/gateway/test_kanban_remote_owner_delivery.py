"""Remote Kanban notices for busy chats the canonical gateway owns ride its FIFO (t_01a1cff0541bf30c).

2026-11-12, David-MBP: four done notices of cards owned by other gateways reached chat
20261026_235722_bef6c6 3-63 min late. The Desktop backend's sweep was the only path; while the gateway ran
the chat's turns back to back, every pass found a turn running and released the claim.

Real replica (hub.db), real remote ledger, real state.db (turn lease, ``session_admissions``, transcript),
the real Desktop-backend sweep and the real gateway pass. Faked at their boundaries: the FIFO admission
(``admit_route_wake`` commits a ``session_admissions`` row like the authority does) and the turn (it claims
the FIFO head and writes the user row from the restored descriptor, as ``run_turn`` does with
``display_kind_for_event`` / ``persisted_user_metadata_for_event``).
"""
import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest

from tests.waiting import async_wait_until
from gateway import kanban_remote_owner as kro
from gateway import session_automation
from gateway.config import Platform
from gateway import kanban_wake_fold
from gateway.kanban_wake_fold import card_metadata
from gateway.platforms.event import MessageEvent
from gateway.response_filters import display_kind_for_event, persisted_user_metadata_for_event
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.session_automation import automation_display_metadata
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import kanban_unloaded as ku
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.gateway.test_kanban_wake_fold import GUIDANCE, _automation, _claim, _drain, _local_chat, _wake

@pytest.mark.parametrize('status', ['done', 'ready'])
def test_remote_blocked_notice_refreshes_replica_only_card(monkeypatch, status):
    from hermes_cli import kanban_replica_freshness as fresh
    tid = 't_01a1d59e5543daa1'
    item = {'ref': f'worker-6/default/default/{tid}', 'state': 'blocked@1794779059',
            'event_key': 'remote:worker-6:default/default/' + tid + ':1',
            'text': 'Remote card blocked\nNext action for the user: Reply A or B\n'
                    'Why only the user: decision\nScope: example\nOpen task: example\nKeep this context'}
    monkeypatch.setattr(fresh, 'newer_rows', lambda *_: {tid: {'status': status, 'summary': 'Waiver completed'}})
    with contextlib.closing(kbc.connect()) as conn:
        assert kb.get_task(conn, tid) is None
    card = kro._metadata(item)['kanban_cards'][0]
    text = kanban_wake_fold._compose([card], 'guidance', strict=True)
    assert f"[kanban] Task {tid} {'completed' if status == 'done' else status}." in text
    assert 'Note: an earlier wake' in text and f'Live status: {status}.' in text
    assert 'Keep this context' in text
    for prefix in ('Next action for the user:', 'Why only the user:', 'Scope:', 'Open task:'):
        assert prefix not in text
    if status == 'done':
        assert 'Result: Waiver completed' in text


def test_remote_blocked_metadata_preserves_premise_and_board():
    item = {'ref': 'worker-6/default/research/t_01a1d59e5543daa1', 'state': 'blocked@1794779059',
            'event_key': 'event', 'text': 'blocked'}
    card = kro._metadata(item)['kanban_cards'][0]
    assert card['kinds'] == ['blocked']
    assert card['board'] == 'research'


@pytest.mark.parametrize(('state', 'kind'), [('done@1', 'completed'),
                                              ('request_withdrawn@1', 'request_withdrawn'), ('other@1', None)])
def test_remote_outcome_metadata_never_relabels(monkeypatch, state, kind):
    item = {'ref': 'worker-6/default/default/t_01a1d59e5543daa1', 'state': state,
            'event_key': 'event', 'text': 'Outcome'}
    card = kro._metadata(item)['kanban_cards'][0]
    assert card['kinds'] == ([kind] if kind else [])
    monkeypatch.setattr(kanban_wake_fold, '_live_task', lambda _: pytest.fail('Outcome must not read live state'))
    assert kanban_wake_fold.refresh_card(card, strict=True) == card
    grouped = kro._metadata({**item, 'children': [item]})['kanban_cards'][0]
    assert grouped['task_id'] == grouped['board'] == '' and grouped['kinds'] == []


@pytest.mark.parametrize('original', ['Remote blocked\nNext action for the user: choose\nContext',
                                       '[kanban] Task t_01a1d59e5543daa1 blocked.\nNext action for the user: choose'])
def test_remote_relabel_preserves_guidance_at_claim(monkeypatch, original):
    item = {'ref': 'worker-6/default/default/t_01a1d59e5543daa1', 'state': 'blocked@1',
            'event_key': 'event', 'text': original}
    card = kro._metadata(item)['kanban_cards'][0]
    monkeypatch.setattr(kanban_wake_fold, '_live_task', lambda _: ('ready', ''))
    text = kanban_wake_fold._compose([card], GUIDANCE)
    assert kanban_wake_fold._guidance(text, [card]) == GUIDANCE
    payload = {'text': text, 'local_automation_v1': {
        'identity': json.dumps(['kanban-wake', 'id']), 'kanban_cards': [card]}}
    monkeypatch.setattr(kanban_wake_fold, '_live_task', lambda _: ('done', 'Finally completed'))
    kanban_wake_fold.refresh_claimed(payload)
    assert 'Result: Finally completed' in payload['text']
    assert payload['text'].endswith(GUIDANCE)


CHAT = "20261026_235722_bef6c6"
GW_HOLDER = f"pid={os.getpid()}:turn={CHAT}:x:platform=local"


def _state_path():
    return get_hermes_home() / "state.db"


class _Live:
    def __init__(self, session_id):
        self.source = SessionSource(platform=Platform.LOCAL, chat_id=session_id, chat_type="dm")
        self.route = build_session_key(self.source)


class _Authority:
    def __init__(self, db):
        self.db = db
        self.sessions = {}

    def logical_owner(self, session_id):
        return session_id


class _Fifo:
    """The chat's durable FIFO in ``session_admissions`` plus the turns that drain it."""

    def __init__(self, db):
        self.db = db
        self.refuse = None
        self.ran = []
        self.seq = 0
        self.row_session = CHAT  # where the turn writes its rows: the lineage tip

    def _write(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(_state_path(), timeout=5)) as conn:
            conn.execute(sql, args)
            conn.commit()

    def admit(self, text, metadata, *, status="queued"):
        descriptor = {"identity": uuid.uuid4().hex, "owner": CHAT, "route": _Live(CHAT).route, "target": CHAT,
                      **automation_display_metadata(metadata), **card_metadata(metadata)}
        payload = json.dumps({"text": text, "local_automation_v1": descriptor})
        self.seq += 1
        epoch = (1, 1) if status == "started" else (None, None)
        self._write("INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, "
                    "lineage_json, payload_json, payload_digest, intent, status, owner_epoch, generation) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, descriptor["identity"], "automation:" + descriptor["route"], CHAT, "[]",
                     payload, uuid.uuid4().hex, "queue", status, *epoch))

    async def admit_route_wake(self, runner, adapter, source, text, producer, metadata=None):
        assert source.chat_id == CHAT and producer == "kanban-wake"
        if isinstance(self.refuse, Exception):
            raise self.refuse
        if self.refuse is False:
            return False
        self.admit(text, dict(metadata or {}))
        return True

    def queued(self):
        with contextlib.closing(sqlite3.connect(_state_path())) as conn:
            return [json.loads(r[0]) for r in conn.execute(
                "SELECT payload_json FROM session_admissions WHERE status='queued' ORDER BY seq")]

    def boundary(self, *, write_row=True):
        """The running turn ends; the FIFO head starts and its turn writes the user row."""
        self._write("UPDATE session_admissions SET status='terminal' WHERE status='started'")
        with contextlib.closing(sqlite3.connect(_state_path())) as conn:
            head = conn.execute("SELECT seq, payload_json FROM session_admissions WHERE status='queued' "
                                "ORDER BY seq LIMIT 1").fetchone()
        if head is None:
            return None
        self._write("UPDATE session_admissions SET status='started', owner_epoch=1, generation=1 WHERE seq=?",
                    (head[0],))
        payload = json.loads(head[1])
        descriptor = payload["local_automation_v1"]
        # session_automation.restore_local_automation's event metadata
        restore = getattr(kanban_wake_fold, "event_keys_metadata", lambda d: {})  # absent before the fix
        metadata = {**automation_display_metadata(descriptor), **restore(descriptor)}
        event = MessageEvent(text=payload["text"], internal=True, metadata=metadata)
        if write_row:
            self.db.append_message(self.row_session, "user", payload["text"], display_kind=display_kind_for_event(event),
                                   display_metadata=persisted_user_metadata_for_event(event) or None)
        self.ran.append(payload["text"])
        return payload["text"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    hub = tmp_path / "hub-state"
    hub.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(hub))
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_UNLOADED_WAKES_PER_HOUR", raising=False)
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()
    kro._reset_for_tests()
    db = SessionDB(_state_path())
    db.create_session(CHAT, "desktop")
    db.append_message(CHAT, "user", "start the cards")
    db.append_message(CHAT, "assistant", "started")
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.LOCAL: object()}
    runner._kanban_notifier_profile = "default"
    runner.session_authorities = None
    runner.session_authority = authority = _Authority(db)
    runner._session_key_for_source = build_session_key
    fifo = _Fifo(db)
    monkeypatch.setattr(session_automation, "admit_route_wake", fifo.admit_route_wake)
    yield hub, runner, authority, fifo, db
    with contextlib.suppress(Exception):
        db.release_session_turn_lease(CHAT, GW_HOLDER)
    db.close()
    krn._reset_for_tests()
    ku._reset_wake_budget_for_tests()
    kro._reset_for_tests()


def _publish(hub, cards):
    with sqlite3.connect(hub / "hub.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS replica(gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, "
                     "data TEXT, updated_at INTEGER, PRIMARY KEY (gw, coll, key))")
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                         [("gateway", "macbook"), ("cursor", "100"), ("head", "100")])
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM replica").fetchone()[0]
        for card in cards:
            seq += 1
            conn.execute("INSERT OR REPLACE INTO replica VALUES (?,'tasks',?,1,?,?,0)",
                         (card.pop("_gw", "worker-2"), f"default/default/{card['id']}", seq, json.dumps(card)))
    from tui_gateway.kanban_remote_observations import observe
    observe(hub / "hub.db", None, time.time() - 60)
    krn._reset_for_tests()


def _done(tid, event_id=466563, *, gw="worker-2", chat=CHAT):
    at = time.time() - 70
    return {"_gw": gw, "id": tid, "status": "done", "title": f"card {tid}", "board": "default",
            "assignee": "default", "completed_at": at,
            "completion_notice": {"event_id": event_id, "created_at": at, "payload": {"summary": f"{tid} verdict"}},
            "sessions": [{"gw": "macbook", "profile": "default", "session": chat, "rel": "shepherd"}]}


def _key(tid, event_id=466563, gw="worker-2"):
    return f"remote:{gw}:default/default/{tid}:{event_id}"


def _busy(db, fifo):
    """A gateway turn runs in the chat: its admission is started and it holds the turn lease."""
    assert db.try_acquire_session_turn_lease(CHAT, GW_HOLDER)
    fifo.admit("human prompt", {}, status="started")


def _sweep(wakes):
    """One Desktop-backend remote pass (``kanban_unloaded._loop``'s ``sweep_remote_once``): the wake is
    refused while a gateway turn holds the chat, as ``_wake_session_turn`` refuses an owned chat."""
    def wake(tip, text, metadata):
        wakes.append(text)
        return "refused"
    return krn.sweep_remote_notices(gateway="macbook", live_ids=set(),
                                    waker=ku._RemoteWaker(wake, lambda job: (job(), "dispatched")[1], 0.0))


def _rows(key):
    with contextlib.closing(sqlite3.connect(_state_path())) as conn:
        return [r[0] for r in conn.execute(
            "SELECT m.id FROM messages m, json_each(json_extract(m.display_metadata, '$.kanban_event_keys')) j "
            "WHERE m.session_id = ? AND m.display_kind = 'kanban_notice' AND json_valid(m.display_metadata) "
            "AND j.value = ?", (CHAT, key))]


def _ledger(tid, gw="worker-2"):
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM notices WHERE session=? AND ref=?",
                           (CHAT, f"{gw}/default/default/{tid}")).fetchone()
        return dict(row) if row else None


def _tick(runner):
    return asyncio.run(kro.tick(runner))


def test_n1_busy_owned_chat_admits_on_first_pass_and_row_lands_at_next_boundary(env, caplog):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])

    admitted = _tick(runner)

    assert [item["ref"] for item in admitted] == ["worker-2/default/default/t_01a1cfabf44499dd"]
    [queued] = fifo.queued()
    assert "t_01a1cfabf44499dd" in queued["text"]
    assert queued["local_automation_v1"]["display_kind"] == "kanban_notice"
    assert queued["local_automation_v1"]["kanban_event_keys"] == [_key("t_01a1cfabf44499dd")]
    claim = _ledger("t_01a1cfabf44499dd")
    assert (claim["delivered"], claim["holder_pid"]) == (0, os.getpid())  # in flight, not released
    assert _rows(_key("t_01a1cfabf44499dd")) == []
    with caplog.at_level(logging.INFO, logger="gateway.kanban_remote_owner"):
        for _ in range(2):  # later passes while it waits in the FIFO: kept in flight, never released or re-admitted
            assert _tick(runner) == []
            assert _ledger("t_01a1cfabf44499dd")["holder_pid"] == os.getpid() and len(fifo.queued()) == 1
    assert [r.getMessage() for r in caplog.records if r.name == "gateway.kanban_remote_owner"] == []

    assert "t_01a1cfabf44499dd" in fifo.boundary()  # the next turn boundary runs it
    assert len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    _tick(runner)
    assert _ledger("t_01a1cfabf44499dd")["delivered"] == krn.DELIVERED
    assert kro._admitted == {}


def test_n2_remote_notice_is_not_starved_by_local_notices_admitted_after_it(env):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    wakes = []
    _sweep(wakes)  # the Desktop backend's pass: refused while the turn runs, the claim goes back
    assert _rows(_key("t_01a1cfabf44499dd")) == []
    assert _tick(runner)
    wakes.clear()
    for i in range(4):  # local notices keep arriving behind it
        fifo.admit(f"[kanban] Task t_local{i} completed.", {"kanban_cards": [
            {"task_id": f"t_local{i}", "board": "default", "kinds": ["completed"], "text": f"local {i}"}]})
    ran = []
    for _ in range(5):  # turns run back to back; passes keep running in between
        _sweep(wakes)
        ran.append(fifo.boundary())
        _tick(runner)
    assert "t_01a1cfabf44499dd" in ran[0]
    assert all("t_local" in text for text in ran[1:])
    assert wakes == []  # once admitted, the sweep never claimed beside the gateway
    assert len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    assert _ledger("t_01a1cfabf44499dd")["delivered"] == krn.DELIVERED


def test_n3a_desktop_pass_after_admission_writes_nothing_and_competes_for_nothing(env, caplog):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    _tick(runner)
    db.release_session_turn_lease(CHAT, GW_HOLDER)  # between turns: the chat looks idle to the sweep
    fifo._write("UPDATE session_admissions SET status='terminal' WHERE status='started'")
    wakes = []
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            _sweep(wakes)
    assert wakes == [] and _rows(_key("t_01a1cfabf44499dd")) == []
    assert not any("deferred" in r.getMessage() for r in caplog.records)
    assert _ledger("t_01a1cfabf44499dd")["holder_pid"] == os.getpid()
    fifo.boundary()
    _sweep(wakes)
    _tick(runner)
    _sweep(wakes)
    assert wakes == [] and len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    assert _ledger("t_01a1cfabf44499dd")["delivered"] == krn.DELIVERED


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _restart(dead):
    """The gateway process dies: its in-process state goes, its ledger claim names a dead pid."""
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        conn.execute("UPDATE notices SET holder_pid=?, holder_start=NULL WHERE delivered=0", (dead,))
        conn.commit()
    kro._reset_for_tests()
    krn._reset_for_tests()


@pytest.mark.parametrize("row_before_successor", [False, True])
def test_n3b_n3c_restart_between_admission_and_row_never_writes_a_second_row(env, row_before_successor):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    _tick(runner)
    db.release_session_turn_lease(CHAT, GW_HOLDER)
    _restart(_dead_pid())
    if row_before_successor:
        fifo.boundary()  # the successor drains its durable FIFO before its first notice pass
    assert db.try_acquire_session_turn_lease(CHAT, GW_HOLDER)
    wakes = []
    _sweep(wakes)
    assert _tick(runner) == []  # adopted or acked by key: never admitted twice
    _sweep(wakes)
    assert len([q for q in fifo.queued() if "t_01a1cfabf44499dd" in q["text"]]) == (0 if row_before_successor else 1)
    if not row_before_successor:
        assert _ledger("t_01a1cfabf44499dd")["holder_pid"] == os.getpid()  # adopted by the successor
        fifo.boundary()
    _tick(runner)
    _sweep(wakes)
    assert wakes == []
    assert len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    assert _ledger("t_01a1cfabf44499dd")["delivered"] == krn.DELIVERED


@pytest.mark.parametrize("failure", [False, RuntimeStoreError("queue_full")])
def test_refused_admission_releases_without_dropping_and_next_pass_admits(env, failure):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    fifo.refuse = failure
    for _ in range(3):
        assert _tick(runner) == []
        assert _ledger("t_01a1cfabf44499dd") is None  # released, not settled
    fifo.refuse = None
    assert len(_tick(runner)) == 1 and len(fifo.queued()) == 1


def test_admission_that_ends_without_its_row_is_offered_again(env):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    _tick(runner)
    fifo.boundary(write_row=False)  # cancelled / failed before the row
    fifo._write("UPDATE session_admissions SET status='terminal' WHERE status='started'")
    fifo.admit("next human prompt", {}, status="started")  # still busy
    assert len(_tick(runner)) == 1  # released (never acked), offered and admitted again
    assert _ledger("t_01a1cfabf44499dd")["delivered"] == 0
    fifo.boundary()
    _tick(runner)
    assert len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    assert _ledger("t_01a1cfabf44499dd")["delivered"] == krn.DELIVERED


def _idle(db, fifo):
    db.release_session_turn_lease(CHAT, GW_HOLDER)
    fifo._write("UPDATE session_admissions SET status='terminal'")


def _sweep_writes_row(db):
    """A Desktop-backend pass on an idle chat: the wake starts a turn that writes the row."""
    def wake(tip, text, metadata):
        db.append_message(tip, "user", text, display_kind="kanban_notice", display_metadata=metadata)
        return "started"
    krn.sweep_remote_notices(gateway="macbook", live_ids=set(),
                             waker=ku._RemoteWaker(wake, lambda job: (job(), "dispatched")[1], 0.0))


def test_n5_wake_fails_until_the_fifo_item_is_dropped_still_one_row_no_claim_in_flight(env):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    _tick(runner)
    for _ in range(3):  # each wake of the admitted item fails and the FIFO drops it undelivered
        fifo._write("UPDATE session_admissions SET status='terminal' WHERE status='queued'")
        _tick(runner)
        assert _ledger("t_01a1cfabf44499dd")["delivered"] == 0  # never acked without the row
    fifo._write("UPDATE session_admissions SET status='terminal' WHERE status='queued'")
    _idle(db, fifo)
    _tick(runner)  # the drop is seen: released; the idle chat is the sweep's
    assert _ledger("t_01a1cfabf44499dd") is None and kro._admitted == {}
    _sweep_writes_row(db)
    _tick(runner)
    _sweep_writes_row(db)
    assert len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    claim = _ledger("t_01a1cfabf44499dd")
    assert claim["delivered"] != 0  # settled: nothing left in flight


def test_n3c_restart_drops_the_admission_before_the_row_still_exactly_once(env):
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    _tick(runner)
    _restart(_dead_pid())
    _idle(db, fifo)  # the dying gateway's FIFO item ended without the row
    assert _tick(runner) == []  # the successor: an idle chat is the sweep's
    _sweep_writes_row(db)  # the dead holder's claim is re-offered and delivered once
    _tick(runner)
    _sweep_writes_row(db)
    assert len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    assert _ledger("t_01a1cfabf44499dd")["delivered"] != 0


def test_idle_owned_chat_and_no_owner_chat_stay_with_the_desktop_sweep(env):
    """Unchanged: no gateway turn runs in the chat, so the sweep wakes it (or writes the row)."""
    hub, runner, authority, _fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)  # owned, idle: no ledger work, no lease
    _publish(hub, [_done("t_01a1cfabf44499dd")])
    assert _tick(runner) == []
    assert _ledger("t_01a1cfabf44499dd") is None
    started = []

    def wake(tip, text, metadata):
        started.append(tip)
        db.append_message(tip, "user", text, display_kind="kanban_notice", display_metadata=metadata)
        return "started"
    krn.sweep_remote_notices(gateway="macbook", live_ids=set(),
                             waker=ku._RemoteWaker(wake, lambda job: (job(), "dispatched")[1], 0.0))
    assert started == [CHAT] and len(_rows(_key("t_01a1cfabf44499dd"))) == 1
    authority.sessions.clear()  # no gateway owner at all: the gateway pass does nothing either
    _publish(hub, [_done("t_01a1cf4eda4b1ad4", 336647, gw="worker-1")])
    assert _tick(runner) == []
    krn.sweep_remote_notices(gateway="macbook", live_ids=set(),
                             waker=ku._RemoteWaker(wake, lambda job: (job(), "dispatched")[1], 0.0))
    assert len(_rows(_key("t_01a1cf4eda4b1ad4", 336647, "worker-1"))) == 1


@pytest.mark.parametrize("persisted", [False, True])
def test_n4_remote_waker_logs_row_written_only_when_persist_wrote_it(caplog, persisted):
    waker = ku._RemoteWaker(lambda tip, text, md: "refused", lambda job: (job(), "dispatched")[1], 0.0)
    ku._reset_wake_budget_for_tests()
    with caplog.at_level(logging.INFO, logger="tui_gateway.kanban_unloaded"):
        waker.deliver("chat", "notice", {}, {"id": "t_x", "status": "done"}, time.time(),
                      persist=lambda: persisted, started=lambda: None)
    lines = [r.getMessage() for r in caplog.records if "remote kanban notice for chat" in r.getMessage()]
    assert lines == ["remote kanban notice for chat (task=t_x): "
                     + ("refused, notice row written" if persisted else "refused, deferred")]


def test_kanban_notice_card_keys_are_validated_and_ride_the_user_row():
    keys = [_key("t_01a1cfabf44499dd")]
    md = {"display_kind": "kanban_notice", "display_text": "✔ done", "kanban_event_keys": keys}
    assert automation_display_metadata(md) == {"display_kind": "kanban_notice", "display_text": "✔ done"}
    assert card_metadata(md) == {"kanban_event_keys": keys}
    event = MessageEvent(text="✔ done", internal=True, metadata=dict(md))
    assert display_kind_for_event(event) == "kanban_notice"
    assert persisted_user_metadata_for_event(event) == {"display_text": "✔ done", "kanban_event_keys": keys}
    for bad in ({**md, "kanban_event_keys": []}, {**md, "kanban_event_keys": [""]}, {**md, "kanban_event_keys": "k"},
                {"display_kind": "process_complete", "display_text": "x", "kanban_event_keys": keys}):
        with pytest.raises(RuntimeStoreError, match="invalid_params"):
            card_metadata(bad)


TIP = f"{CHAT}_c1"


def _lineage_rows(key):
    with contextlib.closing(sqlite3.connect(_state_path())) as conn:
        return [r[0] for r in conn.execute(
            "SELECT m.id FROM messages m, json_each(json_extract(m.display_metadata, '$.kanban_event_keys')) j "
            "WHERE m.session_id IN (?, ?) AND m.display_kind = 'kanban_notice' AND json_valid(m.display_metadata) "
            "AND j.value = ?", (CHAT, TIP, key))]


@pytest.mark.parametrize("first", ["sweep", "gateway"])
def test_link_on_a_compressed_tip_is_delivered_once_whichever_path_goes_first(env, first):
    """Review t_01a1d013e0411032: the chat was compressed before the card was linked, so the link names the
    tip while the gateway owns the chat under its root id. Both paths must claim under the same ledger key."""
    hub, runner, authority, fifo, db = env
    db.end_session(CHAT, "compression")
    db.create_session(TIP, "desktop", parent_session_id=CHAT)
    authority.sessions[CHAT] = _Live(CHAT)
    fifo.row_session = TIP
    _publish(hub, [_done("t_01a1cfabf44499dd", chat=TIP)])
    started = []

    def wake(tip, text, metadata):
        started.append(tip)
        db.append_message(tip, "user", text, display_kind="kanban_notice", display_metadata=metadata)
        return "started"

    def sweep():
        krn.sweep_remote_notices(gateway="macbook", live_ids=set(),
                                 waker=ku._RemoteWaker(wake, lambda job: (job(), "dispatched")[1], 0.0))

    if first == "sweep":
        sweep()  # idle: the sweep wakes the chat, the turn writes the row
        assert started == [TIP]
        _busy(db, fifo)
        assert _tick(runner) == []  # busy now: the gateway never admits it a second time
        assert fifo.queued() == []
    else:
        _busy(db, fifo)
        assert len(_tick(runner)) == 1
        fifo.boundary()  # the row lands at the next turn boundary
        _idle(db, fifo)
        sweep()  # idle again before the gateway's next pass: the sweep never wakes it a second time
        assert started == []
        _tick(runner)
    sweep()
    _tick(runner)
    assert len(_lineage_rows(_key("t_01a1cfabf44499dd"))) == 1
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        states = conn.execute("SELECT session, delivered FROM notices WHERE ref=?",
                              ("worker-2/default/default/t_01a1cfabf44499dd",)).fetchall()
    assert states == [(TIP, krn.DELIVERED)]  # one claim, under the link's session, settled
    assert kro._admitted == {}


def _late(tid, event_id):
    card = _done(tid, event_id)
    at = time.time() - krn.FRESH_S - 3600
    card["completed_at"] = at
    card["completion_notice"]["created_at"] = at
    return card


def test_a_successor_adopts_the_held_member_of_a_late_group_and_releases_the_rest(env):
    """A predecessor admitted late notice A and died; late notice B arrived since. The successor's first pass
    claims both as one group: A is adopted (its FIFO item still holds it), B goes back unclaimed, not left
    in flight under the successor's pid with nothing tracking it."""
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _publish(hub, [])  # the replica's first observation is a baseline; arrivals after it are new
    _busy(db, fifo)
    _publish(hub, [_late("t_01a1cf4eda4b1ad4", 336647)])
    assert len(_tick(runner)) == 1
    _restart(_dead_pid())
    _publish(hub, [_late("t_01a1cfd7578b3af4", 466999)])
    assert _tick(runner) == []
    assert _ledger("t_01a1cf4eda4b1ad4")["holder_pid"] == os.getpid()  # adopted, still in flight
    assert _ledger("t_01a1cfd7578b3af4") is None  # released for the next pass
    assert [entry["claim"]["ref"] for entry in kro._admitted.values()] == ["worker-2/default/default/t_01a1cf4eda4b1ad4"]
    assert len(_tick(runner)) == 1  # B is offered on its own and admitted
    assert len(fifo.queued()) == 2
    fifo.boundary()
    fifo.boundary()
    _tick(runner)
    assert len(_rows(_key("t_01a1cf4eda4b1ad4", 336647))) == 1
    assert len(_rows(_key("t_01a1cfd7578b3af4", 466999))) == 1
    assert _ledger("t_01a1cf4eda4b1ad4")["delivered"] == krn.DELIVERED
    assert _ledger("t_01a1cfd7578b3af4")["delivered"] == krn.DELIVERED
    assert kro._admitted == {}


def _write_notice_row(db, session_id, key):
    """A kanban_notice row carrying ``key``, written by some other path (an earlier root-keyed claim)."""
    db.append_message(session_id, "user", "✔ Remote Kanban card done.", display_kind="kanban_notice",
                      display_metadata={"display_text": "✔ done", "kanban_event_keys": [key]})


def _tip_ledger(tid):
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        return conn.execute("SELECT session, delivered, holder_pid FROM notices WHERE ref=?",
                            (f"worker-2/default/default/{tid}",)).fetchall()


def test_a_notice_already_in_the_lineage_with_no_ledger_row_under_the_link_is_acked_not_admitted(env):
    """Review t_01a1d03acceb95c1: the row is already in the chat's lineage (written under the root's ledger
    key), and the link names the compression tip, which has no ledger row yet. The busy-chat pass claims it
    fresh under the tip; it must ack that claim against the transcript, never admit a second turn."""
    hub, runner, authority, fifo, db = env
    key = _key("t_01a1cfabf44499dd")
    _write_notice_row(db, CHAT, key)
    db.end_session(CHAT, "compression")
    db.create_session(TIP, "desktop", parent_session_id=CHAT)
    authority.sessions[CHAT] = _Live(CHAT)
    fifo.row_session = TIP
    _busy(db, fifo)
    _publish(hub, [_done("t_01a1cfabf44499dd", chat=TIP)])
    assert _tick(runner) == []
    assert fifo.queued() == []
    assert _tip_ledger("t_01a1cfabf44499dd") == [(TIP, krn.DELIVERED, None)]
    assert len(_lineage_rows(key)) == 1
    assert kro._admitted == {}


def test_a_late_group_with_one_member_already_written_acks_it_and_releases_the_rest(env):
    """The partly written late group: the written member is acked from the transcript at claim time (never
    offered: t_01a1db370b459fab), and the other is admitted on its own, so each lands exactly once."""
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _publish(hub, [])  # baseline: arrivals after it are new
    _write_notice_row(db, CHAT, _key("t_01a1cf4eda4b1ad4", 336647))
    _busy(db, fifo)
    _publish(hub, [_late("t_01a1cf4eda4b1ad4", 336647), _late("t_01a1cfd7578b3af4", 466999)])
    admitted = _tick(runner)
    assert len(admitted) == 1  # only the unwritten member
    assert _key("t_01a1cf4eda4b1ad4", 336647) not in json.dumps(admitted)
    assert _ledger("t_01a1cf4eda4b1ad4")["delivered"] == krn.DELIVERED
    fifo.boundary()
    _tick(runner)
    assert len(_rows(_key("t_01a1cf4eda4b1ad4", 336647))) == 1
    assert len(_rows(_key("t_01a1cfd7578b3af4", 466999))) == 1
    assert _ledger("t_01a1cfd7578b3af4")["delivered"] == krn.DELIVERED
    assert kro._admitted == {}


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real local Kanban board (``test_kanban_wake_fold``'s fixture)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))

    def task(title):
        with contextlib.closing(kbc.connect()) as conn:
            return kb.create_task(conn, title=title, assignee="worker")
    return task


def _remote_item(tid, event_id):
    return {"text": f"✔ Remote Kanban card {tid} on worker-2 completed.\nResult: {tid} verdict",
            "ref": f"worker-2/default/default/{tid}", "state": "done", "session": CHAT,
            "event_key": _key(tid, event_id)}


@pytest.mark.asyncio
async def test_local_and_remote_notices_queued_during_one_turn_run_as_one_turn(tmp_path, monkeypatch, board):
    """Orchestrator acceptance (06:24 PST): local A, remote R1, local B, remote R2 admitted during one busy
    turn run as ONE wake turn carrying all four, and that turn's row carries both remote keys once."""
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import list_session_admissions
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        runner = authority.runner
        a, b = board("local A"), board("local B")
        r1, r2 = _remote_item("t_remote_r1", 1001), _remote_item("t_remote_r2", 1002)

        async def remote(item):
            md = kro._metadata(item)
            assert await session_automation.admit_route_wake(runner, adapter, source, md["kanban_cards"][0]["text"],
                                                             "kanban-wake", md)

        await _wake(adapter, source, a, "completed")
        await remote(r1)
        await _wake(adapter, source, b, "completed")
        await remote(r2)
        rows = [row for row in queued() if row["principal_id"].startswith("automation:")]
        assert len(rows) == 1, [row["payload"]["text"][:80] for row in rows]
        text = rows[0]["payload"]["text"]
        assert text.startswith("[IMPORTANT: 4 Kanban task updates arrived while this session was busy.")
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)  # the local wakes' guidance, once, last
        for needle in (f"[kanban] Task {a} completed.", f"[kanban] Task {b} completed.", "card t_remote_r1 ",
                       "card t_remote_r2 "):
            assert text.count(needle) == 1, needle
        from tui_gateway.kanban_remote_fifo import pending_keys
        sid = rows[0]["target_session_id"]
        assert pending_keys(authority.db, {sid}) == {r1["event_key"], r2["event_key"]}  # custody: still held

        # The turn: restored from the stored row as the drain does; its user row carries both keys.
        event = session_automation.restore_local_automation(authority, SessionRef(authority.profile_id, sid), rows[0])
        assert event.metadata["kanban_event_keys"] == [r1["event_key"], r2["event_key"]]
        authority.db.append_message(sid, "user", event.text, display_kind=display_kind_for_event(event),
                                    display_metadata=persisted_user_metadata_for_event(event) or None)
        for key in (r1["event_key"], r2["event_key"]):
            assert krn.notice_in_transcript(authority.db, sid, key, "")
        delivered = [m for m in authority.db.get_messages(sid) if m.get("display_kind") == "kanban_notice"]
        assert len(delivered) == 1
        cancelled = [row for row in list_session_admissions(authority.db, session_id=sid, pending_only=False)
                     if row["status"] != "queued" and row["principal_id"].startswith("automation:")]
        assert cancelled  # the folded-away rows (review t_01a1d03acceb95c1: this list was always empty)
        assert all("kanban_event_keys" not in json.dumps(row["payload"]) for row in cancelled)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_local_wake_folds_into_a_remote_notice_waiting_at_the_tail(tmp_path, monkeypatch, board):
    """A remote row at the FIFO tail no longer stops the next local wake from folding (local behaviour of
    ``kanban_wake_fold`` before this change)."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        md = kro._metadata(_remote_item("t_remote_r1", 1001))
        assert await session_automation.admit_route_wake(authority.runner, adapter, source,
                                                         md["kanban_cards"][0]["text"], "kanban-wake", md)
        a = board("local A")
        await _wake(adapter, source, a, "completed")
        rows = [row for row in queued() if row["principal_id"].startswith("automation:")]
        assert len(rows) == 1
        descriptor = rows[0]["payload"]["local_automation_v1"]
        assert descriptor["display_kind"] == "kanban_notice"
        assert descriptor["kanban_event_keys"] == [_key("t_remote_r1", 1001)]
        assert f"[kanban] Task {a} completed." in rows[0]["payload"]["text"]
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_remote_notice_folding_last_keeps_the_local_wakes_guidance(tmp_path, monkeypatch, board):
    """Review t_01a1d03acceb95c1: local A waits, remote R1 folds into it. R1 carries no guidance, and the
    batch must keep A's "inspect the board, don't re-decompose" line, once, at the end."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        a = board("local A")
        await _wake(adapter, source, a, "completed")
        md = kro._metadata(_remote_item("t_remote_r1", 1001))
        assert await session_automation.admit_route_wake(authority.runner, adapter, source,
                                                         md["kanban_cards"][0]["text"], "kanban-wake", md)
        [row] = [row for row in queued() if row["principal_id"].startswith("automation:")]
        text = row["payload"]["text"]
        assert f"[kanban] Task {a} completed." in text and "card t_remote_r1 " in text
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
    finally:
        authority.db.close()


async def _admit_remote(authority, adapter, source, item):
    md = kro._metadata(item)
    assert await session_automation.admit_route_wake(authority.runner, adapter, source,
                                                     md["kanban_cards"][0]["text"], "kanban-wake", md)


@pytest.mark.asyncio
async def test_a_local_wake_folding_into_a_remote_only_batch_keeps_its_own_guidance(tmp_path, monkeypatch, board):
    """Review t_01a1d0636f0b4b88 (P1): a remote-only batch carries no guidance. A local wake folding into it
    brings its own, which the waiting batch's recovery must not overwrite with the batch's empty tail; a
    remote notice folding after that recovers it, so it appears once, at the end."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        await _admit_remote(authority, adapter, source, _remote_item("t_remote_r1", 1001))
        await _admit_remote(authority, adapter, source, _remote_item("t_remote_r2", 1002))
        [row] = _automation(queued())
        text = row["payload"]["text"]
        assert GUIDANCE not in text and text == text.rstrip()
        await _admit_remote(authority, adapter, source, _remote_item("t_remote_r3", 1003))
        a = board("local A")
        await _wake(adapter, source, a, "completed")
        [row] = _automation(queued())
        assert row["payload"]["text"].count(GUIDANCE) == 1 and row["payload"]["text"].endswith(GUIDANCE)
        await _admit_remote(authority, adapter, source, _remote_item("t_remote_r4", 1004))
        [row] = _automation(queued())
        text = row["payload"]["text"]
        assert f"[kanban] Task {a} completed." in text and "card t_remote_r4 " in text
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
    finally:
        authority.db.close()


@pytest.mark.asyncio
async def test_a_relabelled_local_wake_last_in_a_remote_batch_keeps_guidance_through_a_later_remote(
        tmp_path, monkeypatch, board):
    """Review t_01a1d0636f0b4b88 (P2): a stale local wait (its card claimed before delivery) folds into a
    remote-only batch and is relabelled as the last card. It keeps its guidance, and a remote notice folding
    after it recovers that guidance past the Note line: once, at the end, also after the claim-time drain."""
    authority, adapter, source, queued, _human = await _local_chat(tmp_path, monkeypatch)
    try:
        await _admit_remote(authority, adapter, source, _remote_item("t_remote_r0", 1000))
        a = board("waited on")
        _claim(a)
        await _wake(adapter, source, a, "chat_wait_unclaimed")
        [row] = _automation(queued())
        text = row["payload"]["text"]
        assert "Note: an earlier wake for this card said" in text
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
        await _admit_remote(authority, adapter, source, _remote_item("t_remote_r1", 1001))
        [row] = _automation(queued())
        text = row["payload"]["text"]
        assert f"[kanban] Task {a} claimed by a worker (running)." in text
        assert text.count(GUIDANCE) == 1 and text.endswith(GUIDANCE)
        drained = await _drain(authority, row, monkeypatch)
        assert drained.count(GUIDANCE) == 1 and drained.endswith(GUIDANCE)
    finally:
        authority.db.close()


def test_remote_notice_admitted_while_the_notifier_deliveries_are_blocked(env, monkeypatch):
    """The remote owner pass has its own watcher and cadence (t_01a1d4de28a8510d): it ran inline after the
    notifier's local deliveries, so a slow delivery or a slow pass held both and the MacBook admitted busy-chat
    remote notices 13-88 s after it first saw them. A notifier stuck in a delivery must not hold it back."""
    from gateway import kanban_watchers as kw
    hub, runner, authority, fifo, db = env
    authority.sessions[CHAT] = _Live(CHAT)
    _busy(db, fifo)
    runner._running = True
    runner._KANBAN_WATCHER_START_DELAY = 0.0
    in_delivery = asyncio.Event()
    blocker = asyncio.Event()  # never set while the remote notice is waited for

    class _StuckNotification:
        def __init__(self, *a, **k):
            pass

        async def deliver(self):
            in_delivery.set()
            await blocker.wait()

    monkeypatch.setattr(kw, "_notifier_collect", lambda *a, **k: [{"local": "delivery"}])
    monkeypatch.setattr(kw, "_KanbanNotification", _StuckNotification)

    async def scenario():
        notifier = asyncio.ensure_future(runner._kanban_notifier_watcher(interval=1))
        await asyncio.wait_for(in_delivery.wait(), 5)
        owner = asyncio.ensure_future(runner._kanban_remote_owner_watcher(interval=1))
        _publish(hub, [_done("t_01a1cfabf44499dd")])
        try:
            # within 2 owner-pass intervals (1 s each), plus slack for the pass itself
            await async_wait_until(fifo.queued, timeout=2 * 1.0 + 0.5,
                                   what="remote notice admitted within 2 owner-pass intervals")
            assert not blocker.is_set() and not notifier.done()  # the local delivery is still stuck
        finally:
            runner._running = False
            blocker.set()
            await asyncio.wait_for(asyncio.gather(notifier, owner), 5)  # stopped between passes, not cancelled

    asyncio.run(scenario())
    [queued] = fifo.queued()
    assert "t_01a1cfabf44499dd" in queued["text"]
    assert _ledger("t_01a1cfabf44499dd")["holder_pid"] == os.getpid()  # claim in flight, not stranded


def test_remote_owner_watcher_honours_notify_in_gateway(env, monkeypatch):
    from hermes_cli import config as hc
    _hub, runner, _authority, _fifo, _db = env
    runner._running = True
    runner._KANBAN_WATCHER_START_DELAY = 0.0  # a gate regression fails on ``calls``, not on the 2 s timeout
    calls = []

    async def tick(*a, **k):
        calls.append(a)
        runner._running = False  # one pass, so a gate regression fails on ``calls`` below
        return []
    monkeypatch.setattr(hc, "load_config", lambda: {"kanban": {"notify_in_gateway": False}})
    monkeypatch.setattr(kro, "tick", tick)
    asyncio.run(asyncio.wait_for(runner._kanban_remote_owner_watcher(interval=1), 2))
    assert calls == []


def test_the_notifier_loop_no_longer_runs_the_remote_owner_pass(env, monkeypatch):
    """Exactly one caller of ``kro.tick`` (not re-entrant): the supervised remote owner watcher."""
    from gateway import kanban_watchers as kw
    _hub, runner, _authority, _fifo, _db = env
    runner._running = True
    runner._KANBAN_WATCHER_START_DELAY = 0.0
    calls = []

    async def tick(*a, **k):
        calls.append(a)
        return []

    def collect(*a, **k):
        runner._running = False  # one notifier pass
        return []
    monkeypatch.setattr(kro, "tick", tick)
    monkeypatch.setattr(kw, "_notifier_collect", collect)
    asyncio.run(asyncio.wait_for(runner._kanban_notifier_watcher(interval=1), 5))
    assert calls == []
    assert "_kanban_remote_owner_watcher" in GatewayRunner._PRE_RECONNECT_WATCHERS
