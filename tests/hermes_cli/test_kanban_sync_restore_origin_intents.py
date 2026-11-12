"""A courier card restored from the Board publishes its exact origin marker again. [daveey]

``origin_creation_intents`` is machine-local. An owner reprovisioned from the Board got its courier cards back but
not their intent rows, so the hermes-hub collector published ``origin_bindings: []`` for them and the origin
gateway's ledger never advanced the obligation again (t_01a1cf7ee5a6103e).
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_origin_intent_carry import EVENT_KIND, restore_from_event
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_sync_restore_origin_subs as base
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME = base.BOARD, base.ME
ORIGIN = {"gateway": "macbook", "profile": "default", "session": "sess-a"}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def live(kanban_home, svc):
    conn = base._open(kanban_home, svc)
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        conn.close()


def _intent(request_id="request-restore-001"):
    return {"request_id": request_id, "owner": {"gateway": ME, "profile": "default", "board": BOARD},
            "origin": ORIGIN}


def _bindings(conn, tid):
    """The collector's ``origin_bindings`` (``hermes_hub.py collect_tasks``: ``origin_creation_intents`` rows)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='origin_creation_intents'").fetchone():
        return []
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT binding_json FROM origin_creation_intents WHERE task_id=? ORDER BY request_id", (tid,))]


def _publish(conn, replica, tid, version):
    """Publish the owner record the collector would (observation attached on the owner) into the hub replica."""
    card = kb.get_task(conn, tid)
    assert card is not None
    task = {"id": tid, "status": card.status, "gateway": ME, "profile": "default",
            "board": BOARD, "origin_bindings": _bindings(conn, tid)}
    conn.execute("BEGIN")
    try:
        record = ledger.observe_owner_task(conn, task, now=time.time())
    finally:
        conn.execute("ROLLBACK")
    with sqlite3.connect(replica) as rep:
        rep.execute("CREATE TABLE IF NOT EXISTS replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, data TEXT, "
                    "PRIMARY KEY (gw, coll, key))")
        rep.execute("INSERT OR REPLACE INTO replica VALUES (?, 'tasks', ?, ?, ?)",
                    (ME, f"default/{BOARD}/{tid}", version, json.dumps(record)))
    return record


def _observation(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT observation_version, released FROM origin_obligations").fetchone()


@pytest.mark.parametrize("how", ["create", "adopt"])
def test_restored_courier_card_republishes_its_exact_marker_and_the_obligation_advances(
        live, svc, kanban_home, tmp_path, how):
    if how == "create":
        tid = kb.create_task(live, title="courier card", assignee="worker", origin_intent=_intent())
    else:
        from hermes_cli.kanban_origin_creation import adopt_with_origin
        tid = kb.create_task(live, title="existing card", assignee="worker")
        adopt_with_origin(live, {**_intent(), "owner": {**_intent()["owner"], "task_id": tid}}, board=BOARD)
    [marker] = _bindings(live, tid)
    row = live.execute("SELECT * FROM origin_creation_intents").fetchone()
    before = tuple(row)

    # The origin gateway accepts the binding from the owner's first publication.
    replica, path = tmp_path / "hub-replica.db", tmp_path / "origin-ledger.db"
    _publish(live, replica, tid, 1)
    ack = ledger.accept_binding(path=path, replica=replica, binding={**marker, "publication_version": 1},
                                request_id="receipt-restore-001", sender=f"gw:{ME}", gateway="macbook",
                                profile="default", resolve_session=lambda s: s)
    assert ack["status"] == "accepted"
    assert _observation(path) == (1, 0)
    base._round(kanban_home, svc)

    restored = base._reprovision(kanban_home, svc, live)
    try:
        republished = _publish(restored, replica, tid, 2)
        ledger.refresh_observations(path, replica)
        # Before the fix the record carried origin_bindings=[] (the intent row stayed on the old machine), so the
        # obligation stayed at version 1 forever.
        assert (_observation(path), republished["origin_bindings"]) == ((2, 0), [marker])
        assert tuple(restored.execute("SELECT * FROM origin_creation_intents").fetchone()) == before
        base._round(kanban_home, svc)  # idempotent
        assert restored.execute("SELECT COUNT(*) FROM origin_creation_intents").fetchone()[0] == 1
        if how == "create":  # an exact courier retry after the restore is answered by the request ID, no duplicate
            assert kb.create_task(restored, title="courier card", assignee="worker", origin_intent=_intent()) == tid
            assert restored.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        restored.close()


def test_restore_hook_only_rebuilds_an_own_restored_card_from_a_valid_payload(live):
    tid = kb.create_task(live, title="courier card", assignee="worker", origin_intent=_intent())
    payload = live.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, EVENT_KIND)).fetchone()[0]
    with kb.write_txn(live):
        live.execute("DELETE FROM origin_creation_intents WHERE task_id=?", (tid,))
    cols = {"task_id": tid, "kind": EVENT_KIND, "payload": payload}
    with kb.write_txn(live):
        assert restore_from_event(live, cols, ME) is False  # not a restored card: never resurrected
        live.execute("CREATE TABLE IF NOT EXISTS kanban_restored_cards (task_id TEXT PRIMARY KEY, "
                     "restored_at INTEGER NOT NULL)")
        live.execute("INSERT INTO kanban_restored_cards VALUES (?, ?)", (tid, int(time.time())))
        assert restore_from_event(live, cols, "worker-2") is False  # another gateway's card: its owner rebuilds
        assert restore_from_event(live, {**cols, "task_id": "t_other"}, ME) is False
        assert restore_from_event(live, {**cols, "kind": "created"}, ME) is False
        for bad in ("not json", json.dumps({"v": 2}), json.dumps({**json.loads(payload), "binding_json": "{}"})):
            assert restore_from_event(live, {**cols, "payload": bad}, ME) is False
        assert _bindings(live, tid) == []
        assert restore_from_event(live, cols, ME) is True
        assert restore_from_event(live, cols, ME) is False  # an existing row is never changed
    assert len(_bindings(live, tid)) == 1


def test_event_gc_keeps_the_carried_intent(live):
    tid = kb.create_task(live, title="courier card", assignee="worker", origin_intent=_intent())
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        live.execute("UPDATE task_events SET created_at=0 WHERE task_id=?", (tid,))
    kb.gc_events(live, older_than_seconds=1)
    assert [r[0] for r in live.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))] == [EVENT_KIND]
