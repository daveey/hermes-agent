"""Canonical certification + durable replica of owner reassessment receipts (no suppression)."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from types import SimpleNamespace
from pathlib import Path

import pytest

from hermes_cli import kanban_owner_reassessment_canonical as canonical
from hermes_cli import kanban_owner_reassessment_replica as replica
from hermes_cli.kanban_db_sync import uuid7
from hermes_cli.kanban_owner_reassessment import ReassessmentRefused

BOARD, TASK, GW = "default", "t_0123456789abcdef", "here"


def test_actual_board_evidence_close_reopen_remapped_local_ids(path):
    evidence = json.loads((Path(__file__).parent / 'fixtures' /
                           'board_emitted_owner_reassessment_evidence.json').read_text())
    receipt = evidence['owner_reassessment_v1']
    event = evidence['row']
    c = connect(path)
    replica.certify(c, Hub(evidence), BOARD, task=receipt['task_id'], uid=event['uid'], sender=receipt)
    # Execution DB shape: no deleted_at; deliberately unrelated local integer ids.
    c.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,kind TEXT,created_at INTEGER)')
    c.execute('INSERT INTO task_events VALUES (?,?,?,?,?)',
              (9001, event['uid'], event['task_id'], event['kind'], event['created_at']))
    c.close()
    c = sqlite3.connect(path, isolation_level=None)
    task = {'id': receipt['task_id'], 'status': 'scheduled', 'current_run_id': None}
    assert replica.verified_for_event(c, {**event, 'id': 9001, 'run_id': 777}, task,
                                      now=receipt['issued_at']+10) == receipt
    assert replica.verified_for_event(c, event, task, now=receipt['backstop_at']) is None
    c.close()


@pytest.mark.parametrize('tombstones', [True, False])
def test_latest_park_portable_order_not_remapped_arrival_id(path, tombstones):
    c = connect(path)
    c.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,kind TEXT,created_at INTEGER'
              + (',deleted_at INTEGER' if tombstones else '') + ')')
    second = 1795616194
    # UUIDv7 milliseconds both inside the same created_at second.
    earlier = '01a21310-be50-7000-8000-000000000001'
    later = '01a21310-c100-7000-8000-000000000002'
    from hermes_cli.kanban_sync_divergence import _event_order
    assert _event_order({'uid': later, 'created_at': second, 'id': 1}) > _event_order(
        {'uid': earlier, 'created_at': second, 'id': 999})
    for ident, uid in [(999, earlier), (1, later)]:
        c.execute('INSERT INTO task_events(id,uid,task_id,kind,created_at) VALUES (?,?,?,?,?)',
                  (ident, uid, TASK, 'scheduled', second))
    assert replica.latest_park_uid(c, TASK) == later
    if tombstones:
        c.execute('UPDATE task_events SET deleted_at=1 WHERE uid=?', (later,))
        assert replica.latest_park_uid(c, TASK) == earlier
    c.close()


@pytest.mark.parametrize('ids', [(1, 999), (999, 1)])
def test_latest_park_same_millisecond_never_uses_remapped_ids(path, ids):
    c = connect(path)
    c.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY,uid TEXT,task_id TEXT,kind TEXT,created_at INTEGER)')
    for ident, uid in zip(ids, ['01a21310-be50-7000-8000-000000000001',
                               '01a21310-be50-7000-8000-000000000002']):
        c.execute('INSERT INTO task_events VALUES (?,?,?,?,?)', (ident, uid, TASK, 'scheduled', 1795616194))
    assert replica.latest_park_uid(c, TASK) is None
    c.close()


def make(kind="scheduled"):
    ep, park, run = uuid7(), uuid7(), uuid7()
    payload = json.dumps({"reason": "w", "wake_at": 1795613693, "owner_reassessment_episode_uid": ep,
                          **({"status": "scheduled"} if kind == "continued" else {}),
                          "awaiting": {"kind": "external", "ref": "executor:example", "why": "q", "premise": "x"}})
    receipt = {"version": 1, "board": BOARD, "task_id": TASK, "gateway": GW, "episode_uid": ep,
               "park_event": {"uid": park, "run_uid": run, "kind": kind, "created_at": 1795613573, "payload": payload},
               "issuer": {"owner_admitted_uid": uuid7(), "profile": "default", "run_uid": run,
                          "session_id": "s", "worker_bound_uid": uuid7()},
               "producer": {"kind": "external", "ref": "executor:example", "why": "q"},
               "target": {"kind": "queue", "identity": "executor:example", "revision": "r"},
               "issued_at": 1795613573, "reassess_at": 1795613693, "backstop_at": 1795615373,
               "evidence_refs": [{"table": "task_events", "uid": uuid7()}], "suppresses": ["too_long"],
               "execution_authority": False}
    return receipt


def evidence_for(r):
    p = r["park_event"]
    return {"board": BOARD, "table": "task_events", "uid": p["uid"], "state": "live", "head": 5,
            "row": {"uid": p["uid"], "task_id": r["task_id"], "kind": p["kind"], "payload": p["payload"],
                    "created_at": p["created_at"], "run_uid": p["run_uid"], "origin_gateway": r["gateway"],
                    "deleted_at": None, "seq": 1, "version": 1, "hlc": "a", "updated_at": 1},
            "owner_reassessment_v1": copy.deepcopy(r)}


class Hub:
    def __init__(self, ev, gateway=GW):
        self.ev, self.ident, self.calls = ev, SimpleNamespace(gateway=gateway), 0

    def row_evidence(self, board, table, uid, *, max_response_bytes):
        assert max_response_bytes == canonical.MAX_EVIDENCE_BYTES
        self.calls += 1
        if isinstance(self.ev, Exception):
            raise self.ev
        return copy.deepcopy(self.ev)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "k.db")


def connect(path):
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state(board_id TEXT)")
    if not c.execute("SELECT 1 FROM kanban_sync_state").fetchone():
        c.execute("INSERT INTO kanban_sync_state VALUES (?)", (BOARD,))
    return c


def ch(r, **over):
    p = r["park_event"]
    return SimpleNamespace(table="task_events", op="upsert", uid=p["uid"], origin_gateway=r["gateway"],
                           fields={"task_id": r["task_id"], "kind": p["kind"], "payload": p["payload"],
                                   "created_at": p["created_at"], **over})


def park(c, uid, kind, created_at, deleted_at=None):
    c.execute("CREATE TABLE IF NOT EXISTS task_events(id INTEGER PRIMARY KEY, uid TEXT, task_id TEXT, kind TEXT, "
              "created_at INTEGER, deleted_at INTEGER)")
    c.execute("INSERT INTO task_events(uid,task_id,kind,created_at,deleted_at) VALUES (?,?,?,?,?)",
              (uid, TASK, kind, created_at, deleted_at))


@pytest.mark.parametrize("kind", ["scheduled", "continued"])
def test_sender_certification_close_reopen(path, kind):
    r = make(kind)
    c = connect(path)
    got = replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"], sender=r)
    assert got == r and got["execution_authority"] is False
    c.close()
    c = connect(path)  # reopened: no registration, no memory, proof is durable
    replica.install(c)
    uid = r["park_event"]["uid"]
    assert replica.verified_receipt(c, uid) == r
    event = {"uid": uid, "task_id": TASK, "kind": kind, "payload": r["park_event"]["payload"],
             "created_at": 1795613573}
    park(c, uid, kind, r["park_event"]["created_at"])
    sched = {"id": TASK, "status": "scheduled", "current_run_id": None}
    now = r["issued_at"] + 1
    assert replica.verified_for_event(c, event, sched, now=now) == r
    assert replica.verified_for_event(c, {**event, "payload": event["payload"] + " "}) is None
    assert replica.verified_for_event(c, event, {**sched, "status": "running"}, now=now) is None
    assert replica.verified_for_event(c, {**event, "uid": uuid7()}) is None


def test_remapped_local_run_ids_irrelevant(path):
    r = make()
    ev = evidence_for(r)
    ev["row"]["run_id"] = 999  # a local integer id on the Board row is never identity
    c = connect(path)
    assert replica.certify(c, Hub(ev), BOARD, task=TASK, uid=r["park_event"]["uid"]) == r


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("owner_reassessment_v1"),
    lambda e: e["owner_reassessment_v1"].update(execution_authority=True),
    lambda e: e["owner_reassessment_v1"].update(extra=1),
    lambda e: e["owner_reassessment_v1"].update(gateway="other"),
    lambda e: e["owner_reassessment_v1"].update(board="b2"),
    lambda e: e["owner_reassessment_v1"].update(task_id="t_1123456789abcdef"),
    lambda e: e["owner_reassessment_v1"].update(episode_uid=uuid7()),
    lambda e: e["owner_reassessment_v1"].update(reassess_at=1795613574),
    lambda e: e["owner_reassessment_v1"]["producer"].update(ref="other"),
    lambda e: e["owner_reassessment_v1"]["park_event"].update(run_uid=uuid7()),
    lambda e: e["owner_reassessment_v1"]["park_event"].update(created_at=1),
    lambda e: e["owner_reassessment_v1"]["park_event"].update(kind="blocked"),
    lambda e: e["row"].update(payload="{}"),
    lambda e: e["row"].update(run_uid=uuid7()),
    lambda e: e["row"].update(created_at=1),
    lambda e: e["row"].update(origin_gateway="spoof"),
    lambda e: e["row"].update(origin_gateway=None),
    lambda e: e["row"].update(deleted_at=1),
    lambda e: e["row"].update(task_id="t_1123456789abcdef"),
    lambda e: e.update(state="deleted"),
    lambda e: e.update(state="unknown"),
    lambda e: e.update(uid=uuid7()),
    lambda e: e.update(board="x"),
])
def test_spoofed_missing_changed_identity_refused(path, mutate):
    r = make()
    ev = evidence_for(r)
    mutate(ev)
    c = connect(path)
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(ev), BOARD, task=TASK, uid=r["park_event"]["uid"])
    assert replica.verified_receipt(c, r["park_event"]["uid"]) is None


def test_sender_must_equal_board_and_original_gateway(path):
    r = make()
    c = connect(path)
    forged = {**r, "reassess_at": r["reassess_at"] - 1}
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"], sender=forged)
    with pytest.raises(ReassessmentRefused):  # a different enrolled gateway is not the original sender
        replica.certify(c, Hub(evidence_for(r), gateway="other"), BOARD, task=TASK, uid=r["park_event"]["uid"], sender=r)


def test_unavailable_and_postcommit_uncertainty_record_nothing_then_retry(path):
    r = make()
    uid = r["park_event"]["uid"]
    c = connect(path)
    with pytest.raises(OSError):
        replica.certify(c, Hub(OSError("timeout after send")), BOARD, task=TASK, uid=uid, sender=r)
    assert replica.verified_receipt(c, uid) is None  # unknown never authorizes
    assert replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=uid, sender=r) == r
    assert replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=uid, sender=r) == r  # idempotent
    assert c.execute(f"SELECT COUNT(*) FROM {replica.TABLE}").fetchone()[0] == 1


def test_bookkeeping_restamp_is_same_receipt_but_real_change_conflicts(path):
    r = make()
    uid = r["park_event"]["uid"]
    c = connect(path)
    replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=uid)
    ev = evidence_for(r)
    ev["row"].update(seq=9, version=2, hlc="z", updated_at=7)
    replica.certify(c, Hub(ev), BOARD, task=TASK, uid=uid)
    ev["row"]["created_at"] = 1795613574
    ev["owner_reassessment_v1"]["park_event"]["created_at"] = 1795613574
    with pytest.raises(replica.ReassessmentConflict):
        replica.certify(c, Hub(ev), BOARD, task=TASK, uid=uid)


def test_append_only_direct_sql_protection(path):
    r = make()
    uid = r["park_event"]["uid"]
    c = connect(path)
    replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=uid)
    row = c.execute(f"SELECT * FROM {replica.TABLE}").fetchone()
    for sql, args in (
        (f"UPDATE {replica.TABLE} SET gateway='x'", ()),
        (f"DELETE FROM {replica.TABLE}", ()),
        (f"INSERT OR REPLACE INTO {replica.TABLE} VALUES (?,?,?,?,?,?,?)", row),
        (f"INSERT OR REPLACE INTO {replica.TABLE}(rowid,event_uid,board,task_id,episode_uid,run_uid,gateway,evidence) "
         "VALUES (1,?,?,?,?,?,?,?)", (uuid7(), *row[1:3], uuid7(), *row[4:])),
        (f"INSERT INTO {replica.TABLE} VALUES (?,?,?,?,?,?,?)", (uuid7(), *row[1:3], uuid7(), *row[4:])),
    ):
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql, args)
    # a connection that never registered the permit cannot write at all
    c2 = sqlite3.connect(path, isolation_level=None)
    with pytest.raises(sqlite3.DatabaseError):
        c2.execute(f"DELETE FROM {replica.TABLE}")
    assert c.execute(f"SELECT COUNT(*) FROM {replica.TABLE}").fetchone()[0] == 1
    # even a registered connection outside the private permit refuses a forged insert
    with pytest.raises(sqlite3.DatabaseError):
        c.execute(f"INSERT INTO {replica.TABLE} VALUES (?,?,?,?,?,?,?)", (uuid7(), *row[1:3], uuid7(), *row[4:]))


def test_tampered_stored_evidence_is_not_trusted_on_read(path):
    r = make()
    uid = r["park_event"]["uid"]
    c = connect(path)
    replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=uid)
    c.execute(f"DROP TRIGGER {replica.TABLE}_update")  # an attacker with raw schema access
    ev = evidence_for(r)
    ev["owner_reassessment_v1"]["execution_authority"] = True
    c.execute(f"UPDATE {replica.TABLE} SET evidence=?", (json.dumps(ev),))
    assert replica.verified_receipt(c, uid) is None
    other = connect(path + "2")
    other.execute("UPDATE kanban_sync_state SET board_id='b2'")
    other.close()


def test_board_mismatch_and_oversize(path):
    r = make()
    c = connect(path)
    c.execute("UPDATE kanban_sync_state SET board_id='b2'")
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"])
    big = evidence_for(r)
    big["pad"] = "x" * canonical.MAX_EVIDENCE_BYTES
    c.execute("UPDATE kanban_sync_state SET board_id=?", (BOARD,))
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(big), BOARD, task=TASK, uid=r["park_event"]["uid"])


def test_certify_refused_inside_writer_txn(path):
    r = make()
    c = connect(path)
    c.execute("BEGIN IMMEDIATE")
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"])
    c.execute("ROLLBACK")


def test_page_handling_and_pickup_adoption(path):
    r, plain = make(), make()
    c = connect(path)
    # lookalike: a park event claiming an episode with no Board attestation records nothing
    lookalike = evidence_for(plain)
    lookalike.pop("owner_reassessment_v1")
    replica.certify_page(c, Hub(lookalike), BOARD, [ch(plain)])
    assert replica.verified_receipt(c, plain["park_event"]["uid"]) is None
    # an unrelated event is never fetched
    hub = Hub(evidence_for(r))
    replica.certify_page(c, hub, BOARD, [SimpleNamespace(table="task_events", op="upsert", uid=uuid7(),
                                                          origin_gateway=None, fields={"kind": "created", "payload": "{}"})])
    assert hub.calls == 0
    # a feed row that differs from the canonical attestation refuses the page
    with pytest.raises(replica.ReassessmentConflict):
        replica.certify_page(c, hub, BOARD, [ch(r, created_at=5)])
    # transport failure aborts
    with pytest.raises(OSError):
        replica.certify_page(c, Hub(OSError("down")), BOARD, [ch(r)])
    replica.certify_page(c, hub, BOARD, [ch(r)])
    assert replica.verified_receipt(c, r["park_event"]["uid"]) == r
    # offline pickup copy re-certifies from the source replica
    live = connect(path + ".live")
    replica.adopt_receipts(live, c, [ch(r)])
    assert replica.verified_receipt(live, r["park_event"]["uid"]) == r
    empty = connect(path + ".empty")
    replica.adopt_receipts(empty, c, [ch(plain)])  # nothing carried -> nothing authorized
    assert replica.verified_receipt(empty, plain["park_event"]["uid"]) is None


def test_actual_core_emitted_receipt_shape(path):
    """Receipt copied verbatim from core's emitted fixture (pending.receipt), Board-shaped around it."""
    r = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures",
                                    "core_emitted_owner_reassessment_receipt.json")))
    ev = evidence_for(r)
    c = connect(path)
    assert replica.certify(c, Hub(ev), BOARD, task=r["task_id"], uid=r["park_event"]["uid"], sender=r) == r
    ev["board"] = r["board"]
    assert canonical.certified_receipt(ev, board=r["board"], task=r["task_id"], uid=r["park_event"]["uid"],
                                       sender=r) == r


def certified(path, r=None):
    r = r or make()
    c = connect(path)
    replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"])
    return c, r, {"uid": r["park_event"]["uid"], "task_id": TASK, "kind": r["park_event"]["kind"],
                  "payload": r["park_event"]["payload"], "created_at": r["park_event"]["created_at"]}


def test_current_matches_rejects_historical_superseded_and_expired(path):
    c, r, event = certified(path)
    uid = event["uid"]
    sched = {"id": TASK, "status": "scheduled", "current_run_id": None}
    now = r["issued_at"] + 1
    # no park row in the replica / unreadable: fail closed
    assert replica.verified_for_event(c, event, sched, now=now) is None
    park(c, uid, "scheduled", event["created_at"])
    assert replica.verified_for_event(c, event, sched, now=now) == r
    # deadline: backstop passed, wake_at disagreement, missing now
    assert replica.verified_for_event(c, event, sched, now=r["backstop_at"]) is None
    assert replica.verified_for_event(c, event, {**sched, "wake_at": r["reassess_at"]}, now=now) == r
    assert replica.verified_for_event(c, event, {**sched, "wake_at": r["backstop_at"]}, now=now) is None
    assert replica.verified_for_event(c, event, sched) is None
    # wrong card
    assert replica.verified_for_event(c, event, {**sched, "id": "t_1123456789abcdef"}, now=now) is None
    # a later, similar park of the same card supersedes (never reuses) the historical receipt
    park(c, uuid7(), "scheduled", event["created_at"] + 5)
    assert replica.verified_for_event(c, event, sched, now=now) is None
    assert replica.verified_receipt(c, uid) == r  # still historical proof, never current authority


def test_deleted_newer_park_does_not_supersede_and_run_blocks(path):
    c, r, event = certified(path)
    park(c, event["uid"], "scheduled", event["created_at"])
    park(c, uuid7(), "scheduled", event["created_at"] + 5, deleted_at=9)
    sched = {"id": TASK, "status": "scheduled", "current_run_id": None}
    assert replica.verified_for_event(c, event, sched, now=r["issued_at"] + 1) == r
    assert replica.verified_for_event(c, event, {**sched, "current_run_id": 3}, now=r["issued_at"] + 1) is None


@pytest.mark.parametrize("mutate", [
    lambda r: r["target"].update(kind="bogus"),
    lambda r: r["target"].update(revision=""),
    lambda r: r["target"].update(extra=1),
    lambda r: r.update(target=None),
    lambda r: r.update(evidence_refs=[]),
    lambda r: r.update(evidence_refs=[{"table": "tasks", "uid": uuid7()}]),
    lambda r: r.update(evidence_refs=[{"table": "task_events", "uid": "nope"}]),
    lambda r: r["issuer"].update(profile=""),
    lambda r: r.update(episode_uid=r["park_event"]["uid"]),
])
def test_target_deadline_refs_are_validated_exactly(path, mutate):
    r = make()
    mutate(r)
    c = connect(path)
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"])


def test_duplicate_key_and_nonfinite_payload_refused(path):
    r = make()
    r["park_event"]["payload"] = r["park_event"]["payload"][:-1] + ', "wake_at": 1}'
    c = connect(path)
    with pytest.raises(ReassessmentRefused):
        replica.certify(c, Hub(evidence_for(r)), BOARD, task=TASK, uid=r["park_event"]["uid"])
    with pytest.raises(ReassessmentRefused):
        canonical.parse_evidence('{"a": NaN}')
    with pytest.raises(ReassessmentRefused):
        canonical.parse_evidence("x" * (canonical.MAX_EVIDENCE_BYTES + 1))


def test_oversize_stored_or_pickup_text_is_not_parsed(path):
    c, r, event = certified(path)
    c.execute(f"DROP TRIGGER {replica.TABLE}_update")
    c.execute(f"UPDATE {replica.TABLE} SET evidence=?", ("[" * (canonical.MAX_EVIDENCE_BYTES + 5),))
    assert replica.verified_receipt(c, event["uid"]) is None
    live = connect(path + ".live")
    replica.adopt_receipts(live, c, [ch(r)])  # corrupt source row: nothing carried, no crash
    assert replica.verified_receipt(live, event["uid"]) is None


def test_foreign_board_receipts_not_adopted_or_read(path):
    c, r, event = certified(path)
    other = connect(path + ".b2")
    other.execute("UPDATE kanban_sync_state SET board_id='b2'")
    with pytest.raises(ReassessmentRefused):
        replica.adopt_receipts(other, c, [ch(r)])
    assert replica.verified_receipt(other, event["uid"]) is None
    c.execute("UPDATE kanban_sync_state SET board_id='b2'")  # same file re-labelled: stored proof unusable
    assert replica.verified_receipt(c, event["uid"]) is None


def test_permit_is_connection_private(path):
    c, _r, _event = certified(path)
    row = c.execute(f"SELECT * FROM {replica.TABLE}").fetchone()
    c2 = connect(path)
    replica.install(c2)  # registers its own permit; it is never granted for c2's writes
    forged = (uuid7(), *row[1:3], uuid7(), *row[4:])
    c2.execute("BEGIN IMMEDIATE")
    token = replica._PERMIT.set(c)  # another connection's permit does not open c2
    try:
        with pytest.raises(sqlite3.DatabaseError):
            c2.execute(f"INSERT INTO {replica.TABLE} VALUES (?,?,?,?,?,?,?)", forged)
    finally:
        replica._PERMIT.reset(token)
        c2.execute("ROLLBACK")
