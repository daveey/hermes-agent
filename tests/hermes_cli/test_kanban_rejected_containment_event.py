"""A containment-refused metadata classification retires through normal sync; nothing is replayed. [daveey]

Shape of the live MacBook op 01a204b7-1ceb-7f53-8439-eede265e3dab: a ``blocked`` metadata-only human_request event
on a triage card held by containment, refused ``containment_hold``, frozen identity ambiguous, no tombstone, no
sync_rejected audit, Board ``never_existed``/floor 0, task row already back at the canonical (request-free) state.
"""
import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_release import CONTAINMENT
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARD, ME = "default", "macbook"
REQUEST_UID = "01a20000-0000-7000-8000-000000000001"
AUTHORITY = {"board": BOARD, "gateway": "hub", "task_id": "t_01a1f8b56d1a69fc"}
REQUEST = {"action": "Keep this contained card parked, or archive it?", "why_human": "Only the origin may release",
           "interaction": "approval", "scope": "This card only", "evidence": "The containment episode still holds",
           "choices": [{"label": "Keep", "value": "keep"}, {"label": "Archive", "value": "archive"}]}


@pytest.fixture
def svc(monkeypatch):
    # The real Board's /evidence names its board; containment certification on pull checks it.
    from tests.hermes_cli.kanban_sync_fake_board import FakeHub
    original = FakeHub.row_evidence

    def evidence(self, board, table, uid):
        value = {**original(self, board, table, uid), "board": board}
        row = value.get("row") or {}
        if table == "task_events" and row.get("kind") == "contained":  # the Board's stop attestation
            from hermes_cli.kanban_containment_state import _stop, fingerprint
            value["containment_v1"] = {"version": 1, "episode": uid, "request": REQUEST_UID,
                                       "gateway": row.get("origin_gateway"),
                                       "containment_sha256": fingerprint(_stop(row))}
        return value

    monkeypatch.setattr(FakeHub, "row_evidence", evidence)
    return FakeService()


@pytest.fixture
def live(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    client.ensure_identity(conn, BOARD, ME)
    conn.home = home
    try:
        yield conn
    finally:
        conn.close()


def _contained(live, svc):
    tid = kb.create_task(live, title="contained", assignee="worker")
    client.push(live, svc.hub(ME), BOARD, ME, primary=False)
    sync.set_mode(live, "primary")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(live, tid, "contained", {
            "policy": {"authority": AUTHORITY, "version": 1}, "producer_baseline": None,
            "target": {"board": BOARD, "gateway": ME, "task_id": tid}, "version": 1})
        episode = live.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='contained'", (tid,)).fetchone()[0]
        live.execute("UPDATE tasks SET containment_episode=? WHERE id=?", (episode, tid))
    client.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert client.status(live)["rejected"] == 0 and client.status(live)["pending"] == 0
    # The real Board derives the marker from the accepted stop; the fake stores rows verbatim.
    svc.board(BOARD)["rows"]["tasks"][tid]["containment_episode"] = episode
    return tid, episode


def _old_producer_refused(live, tid):
    """What classify_parked_request wrote before it refused contained cards, as the Board then refused it."""
    with kbc.write_txn(live):
        kb._append_event(live, tid, "blocked", {
            "human_request": REQUEST, "kind": None, "reason": "Parked in triage with no human request",
            "actor": "user", "classified_in_place": True, "metadata_only": True})
        uid = live.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()[0]
        live.execute("UPDATE kanban_sync_outbox SET state='rejected', last_error=?, attempts=2, "
                     "identity_state='ambiguous' WHERE table_name='task_events' AND row_uid=?", (CONTAINMENT, uid))
    op = dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE row_uid=?", (uid,)).fetchone())
    assert op["state"] == "rejected" and client.status(live)["rejected"] == 1
    return op


@pytest.fixture
def refused(live, svc):
    tid, episode = _contained(live, svc)
    return live, tid, episode, _old_producer_refused(live, tid)


def test_normal_sync_retires_with_immutable_receipt_and_touches_nothing_else(refused, svc):
    live, tid, episode, op = refused
    event_before = dict(live.execute("SELECT * FROM task_events WHERE uid=?", (op["row_uid"],)).fetchone())
    task_before = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    canonical_before = copy.deepcopy(svc.board(BOARD)["rows"]["tasks"][tid])

    result = client.sync_board(BOARD, live.home / "kanban.db", svc.hub(ME), ME)

    report, = [r for r in result.rejection_resolutions if r["op_id"] == op["op_id"]]
    assert (report["classification"], report["action"]) == ("containment_refused_classification", "retired")
    assert client.status(live)["rejected"] == 0
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id=?", (op["op_id"],)).fetchone() is None
    # The local history, the card and the containment are exactly as they were; the Board never got the event.
    assert dict(live.execute("SELECT * FROM task_events WHERE uid=?", (op["row_uid"],)).fetchone()) == event_before
    task_after = dict(live.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert {k: task_after[k] for k in ("status", "containment_episode", "human_request", "sync_version")} == {
        k: task_before[k] for k in ("status", "containment_episode", "human_request", "sync_version")}
    assert task_after["containment_episode"] == episode
    assert op["row_uid"] not in svc.board(BOARD)["rows"]["task_events"]
    assert svc.board(BOARD)["rows"]["tasks"][tid] == canonical_before
    receipt = dict(live.execute("SELECT * FROM kanban_rejected_containment_event_receipts").fetchone())
    snapshot = json.loads(receipt["snapshot_json"])
    assert receipt["op_id"] == op["op_id"] and snapshot["operation"] == op
    assert snapshot["lifecycle_authority"] is False
    for sql in ("DELETE FROM kanban_rejected_containment_event_receipts",
                "UPDATE kanban_rejected_containment_event_receipts SET snapshot_sha256='bad'",
                "INSERT OR REPLACE INTO kanban_rejected_containment_event_receipts "
                "SELECT * FROM kanban_rejected_containment_event_receipts"):
        with pytest.raises(sqlite3.IntegrityError, match="rejected_containment_event_immutable"):
            live.execute(sql)
    # Idempotent: a second round has nothing left to resolve and the diff is clean.
    again = client.sync_board(BOARD, live.home / "kanban.db", svc.hub(ME), ME)
    assert not [r for r in again.rejection_resolutions if r["op_id"] == op["op_id"]]
    shadow = client.open_shadow(BOARD, ME)
    try:
        assert client.diff(live, shadow, ME)["clean"]
    finally:
        shadow.close()


def test_existing_receipt_keeps_the_rejection(refused, svc):
    from hermes_cli import kanban_rejected_containment_event as ce
    from hermes_cli.kanban_sync_rejections import _inputs, resolve_rejections
    live, _, _, op = refused
    with kbc.write_txn(live):
        assert ce.archive(live, op, _inputs(live, op), {}, {})
    report, = resolve_rejections(live, svc.hub(ME), BOARD)
    assert (report["classification"], report["action"]) == ("unresolved", "retained")
    assert client.status(live)["rejected"] == 1


@pytest.fixture
def inputs(refused, svc):
    from hermes_cli import kanban_rejected_containment_event as ce
    from hermes_cli.kanban_sync_rejections import _inputs
    live, tid, _episode, op = refused
    hub = svc.hub(ME)
    before = _inputs(live, op)
    evidence = hub.row_evidence(BOARD, "task_events", op["row_uid"])
    parent = hub.row_evidence(BOARD, "tasks", tid)
    assert ce.moot(op, before, evidence, parent)
    return op, before, evidence, parent


def _fields(op, **changes):
    fields = json.loads(op["fields"])
    fields.update(changes)
    op["fields"] = json.dumps(fields)
    return fields


def _payload(op, before, **changes):
    fields = json.loads(op["fields"])
    payload = json.loads(fields["payload"])
    payload.update(changes)
    payload = {k: v for k, v in payload.items() if v is not ...}
    fields["payload"] = json.dumps(payload)
    op["fields"] = json.dumps(fields)
    before["fields"] = fields


VETOES = [
    "table", "operation", "state", "reason", "pending", "tombstone", "receipt", "parent_ops", "not_held", "no_stamp",
    "local_differs", "extra_field", "kind", "run_uid", "task_id", "created_at_type", "payload_not_json",
    "not_metadata_only", "not_classified", "no_human_request", "no_row", "row_uid", "no_parent", "parent_status",
    "no_episode", "local_request", "local_run", "local_claim", "version_type",
    "remote_live", "remote_floor", "remote_row", "remote_tomb",
    "parent_missing", "parent_not_live", "parent_tomb", "parent_uid", "parent_version", "parent_status_differs",
    "parent_episode", "parent_request", "parent_run", "parent_lease",
    "no_start", "two_starts", "start_after_event", "released", "closed", "unattributed_end",
]


@pytest.mark.parametrize("veto", VETOES)
def test_every_predicate_fails_closed(inputs, veto):
    from hermes_cli import kanban_rejected_containment_event as ce
    op, before, evidence, parent = copy.deepcopy(inputs)
    stamp, local, remote = before["containment_event"], before["parent"], parent.get("row")
    episode = local["containment_episode"]
    start = next(e for e in stamp["events"] if e["kind"] == "contained")
    simple = {
        "table": lambda: op.update(table_name="task_runs"), "operation": lambda: op.update(op="compat"),
        "state": lambda: op.update(state="pending"), "reason": lambda: op.update(last_error="containment_hold: other"),
        "pending": lambda: before.update(pending=[("x",)]), "tombstone": lambda: before.update(tombstones=[("x",)]),
        "receipt": lambda: before.update(receipt={"op": "x"}), "parent_ops": lambda: before.update(parent_ops=[("x",)]),
        "not_held": lambda: stamp.update(held=False), "no_stamp": lambda: before.update(containment_event=None),
        "local_differs": lambda: before["fields"].update(created_at=1),
        "extra_field": lambda: before.update(fields=_fields(op, extra=1)),
        "kind": lambda: before.update(fields=_fields(op, kind="completed")),
        "run_uid": lambda: before.update(fields=_fields(op, run_uid="run")),
        "task_id": lambda: before.update(fields=_fields(op, task_id="t_other")),
        "created_at_type": lambda: before.update(fields=_fields(op, created_at="1")),
        "payload_not_json": lambda: before.update(fields=_fields(op, payload="not json")),
        "not_metadata_only": lambda: _payload(op, before, metadata_only=...),
        "not_classified": lambda: _payload(op, before, classified_in_place=False),
        "no_human_request": lambda: _payload(op, before, human_request=None),
        "no_row": lambda: before.update(row=None), "row_uid": lambda: before["row"].update(uid="other"),
        "no_parent": lambda: before.update(parent=None), "parent_status": lambda: local.update(status="todo"),
        "no_episode": lambda: local.update(containment_episode=None),
        "local_request": lambda: local.update(human_request=json.dumps(REQUEST)),
        "local_run": lambda: local.update(current_run_id=7), "local_claim": lambda: local.update(claim_lock="c"),
        "version_type": lambda: local.update(sync_version=str(local["sync_version"])),
        "remote_live": lambda: evidence.update(state="live"), "remote_floor": lambda: evidence.update(floor=3),
        "remote_row": lambda: evidence.update(row={}), "remote_tomb": lambda: evidence.update(tombstone={}),
        "parent_missing": lambda: None, "parent_not_live": lambda: parent.update(state="tombstoned"),
        "parent_tomb": lambda: parent.update(tombstone={}), "parent_uid": lambda: remote.update(uid="t_other"),
        "parent_version": lambda: remote.update(version=remote["version"] + 1),
        "parent_status_differs": lambda: remote.update(status="blocked"),
        "parent_episode": lambda: remote.update(containment_episode="another"),
        "parent_request": lambda: remote.update(human_request=json.dumps(REQUEST)),
        "parent_run": lambda: remote.update(current_run_uid="run"),
        "parent_lease": lambda: remote.update(lease_holder="mini"),
        "no_start": lambda: start.update(uid="another"), "two_starts": lambda: stamp["events"].append(dict(start)),
        "start_after_event": lambda: start.update(created_at=json.loads(op["fields"])["created_at"] + 1),
        "released": lambda: stamp["events"].append({"uid": "r", "kind": "containment_released", "created_at": 1,
                                                     "payload": json.dumps({"episode": episode})}),
        "closed": lambda: stamp["events"].append({"uid": "c", "kind": "containment_closed", "created_at": 1,
                                                   "payload": json.dumps({"episode": episode})}),
        "unattributed_end": lambda: stamp["events"].append({"uid": "u", "kind": "containment_released",
                                                             "created_at": 1, "payload": "{}"}),
    }
    simple[veto]()
    if veto == "parent_missing":
        parent = None
    assert not ce.moot(op, before, evidence, parent), veto


def test_an_unrelated_episode_end_does_not_veto(inputs):
    from hermes_cli import kanban_rejected_containment_event as ce
    op, before, evidence, parent = copy.deepcopy(inputs)
    before["containment_event"]["events"].append({"uid": "r", "kind": "containment_released", "created_at": 1,
                                                  "payload": json.dumps({"episode": "an-earlier-episode"})})
    assert ce.moot(op, before, evidence, parent)


@pytest.mark.parametrize("race", ["event", "task", "containment", "outbox"])
def test_local_change_during_evidence_read_keeps_the_rejection(refused, svc, monkeypatch, race):
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, tid, _episode, op = refused
    hub = svc.hub(ME)
    original = hub.row_evidence

    def read(board, table, uid):
        evidence = original(board, table, uid)
        if table == "task_events":
            with kbc.write_txn(live):
                live.execute(*{
                    "event": ("UPDATE task_events SET created_at=created_at+1 WHERE uid=?", (op["row_uid"],)),
                    "task": ("UPDATE tasks SET priority=priority+1 WHERE id=?", (tid,)),
                    "containment": ("INSERT INTO task_events(task_id,kind,payload,created_at,uid) "
                                    "VALUES (?,'containment_released',?,1,'x')", (tid, json.dumps({"episode": "e"}))),
                    "outbox": ("UPDATE kanban_sync_outbox SET attempts=attempts+1 WHERE op_id=?", (op["op_id"],)),
                }[race])
        return evidence

    monkeypatch.setattr(hub, "row_evidence", read)
    report, = [r for r in resolve_rejections(live, hub, BOARD) if r["op_id"] == op["op_id"]]
    assert report["classification"] == "unresolved" and report["action"] == "retained"
    assert live.execute("SELECT state FROM kanban_sync_outbox WHERE op_id=?", (op["op_id"],)).fetchone()[0] == "rejected"
    assert not live.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_rejected_containment_event_receipts'"
                            ).fetchone() or not live.execute(
        "SELECT 1 FROM kanban_rejected_containment_event_receipts").fetchone()


def test_released_containment_keeps_the_rejection_end_to_end(refused, svc):
    """After the episode ends the refusal reason is gone; retirement needs a different proof, never a replay."""
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    live, tid, episode, op = refused
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_events(task_id,kind,payload,created_at,uid) "
                     "VALUES (?,'containment_released',?,?,?)",
                     (tid, json.dumps({"episode": episode}), 2_000_000_000, sync.uuid7()))
    report, = [r for r in resolve_rejections(live, svc.hub(ME), BOARD) if r["op_id"] == op["op_id"]]
    assert report["classification"] == "unresolved" and client.status(live)["rejected"] >= 1


def test_producer_refuses_to_classify_a_contained_card(live, svc):
    from hermes_cli.kanban_human_request_migration import classify_parked_request
    tid, _ = _contained(live, svc)
    events = live.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    assert classify_parked_request(live, tid, human_request=REQUEST, actor="user", reason="needs a decision") is False
    assert live.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0] == events
    assert live.execute("SELECT human_request FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None
    assert client.status(live)["pending"] == 0


def test_producer_still_classifies_an_uncontained_parked_card(live):
    from hermes_cli.kanban_human_request_migration import classify_parked_request
    tid = kb.create_task(live, title="parked", assignee="worker")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    assert classify_parked_request(live, tid, human_request=REQUEST, actor="user", reason="needs a decision") is True
    assert live.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
