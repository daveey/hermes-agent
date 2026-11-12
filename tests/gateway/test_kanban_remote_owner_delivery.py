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
            self.db.append_message(CHAT, "user", payload["text"], display_kind=display_kind_for_event(event),
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
    return asyncio.run(kro.tick(runner, force=True))


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
