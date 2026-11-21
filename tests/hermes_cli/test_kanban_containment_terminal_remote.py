"""Ordinary canonical archive delivery with durable request custody and exact readback."""
import copy
import json


import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_terminal_replica as fixtures
from tests.hermes_cli.test_kanban_containment_capability import CAPABILITY

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


class ArchiveHub:
    """Only transport is mocked; real core archive, receipt cache and projection run."""
    def __init__(self, conn):
        self.conn = conn
        self.target, self.child, self.stop, self.close_event, self.source = fixtures.scenario(conn)
        self.ident = self.source.ident
        self.capability = {**CAPABILITY, "terminal_archive": 1}
        self.posts = []
        self.closed = False
        self.fail = False
        self.no_proof = False
        self.audit = None  # the archived audit event row the Board "wrote"; None until the archive op arrives
        self.audit_payload = None  # override: a tampered canonical payload
        self.snapshots = 0
        self.task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (self.target,)).fetchone())
        self.task.update(uid=self.target, status="archived", containment_episode=self.stop["event_uid"],
                         origin_gateway="worker", created_on="worker", current_run_uid=None, version=100)

    def containment_capability(self, board):
        assert not self.conn.in_transaction
        return self.capability

    def close(self):
        self.closed = True

    def ops(self, board, ops):
        assert not self.conn.in_transaction
        assert board == "fixture"
        assert ops[0]["op"] == "archive" and ops[0]["task"] == self.target
        self.posts.append(copy.deepcopy(ops))
        self.source.events[self.close_event["event_uid"]]["containment_v1"]["request"] = ops[0]["client_op_id"]
        if self.audit is None:  # independent restatement of the Board merge: extras, via, source_status last
            meta = ops[0].get("archive_metadata") or {"via": None, "event_extra": None}
            payload = {k: v for k, v in (meta["event_extra"] or {}).items() if k not in ("source_status", "via")}
            if isinstance(meta["via"], str):
                payload["via"] = meta["via"]
            payload["source_status"] = "triage"
            self.audit = {**copy.deepcopy(self.source.events[self.stop["event_uid"]]["row"]),
                          "uid": sync.uuid7(), "kind": "archived", "payload": json.dumps(payload)}
        if self.fail:
            raise TimeoutError("archive applied but response lost")
        return {"results": [{"client_op_id": ops[0]["client_op_id"], "status": "applied"}]}

    def snapshot_page(self, board, table, after="", limit=500):
        self.snapshots += 1  # the readback must never scan the board
        raise AssertionError("full board snapshot scan is forbidden")

    def containment_context(self, board, task, producer=None):
        assert not self.conn.in_transaction and task == self.target
        return {"version": 1, "board": board, "target": {"board": board, "gateway": "worker", "task_id": task},
                "status": "archived", "held": True, "producer": None, "producer_baseline": None,
                "containment_events": [self.stop["event_uid"], self.close_event["event_uid"]],
                "archive_event": self.audit["uid"] if self.audit else None}

    def row_evidence(self, board, table, uid):
        assert not self.conn.in_transaction
        if table == "tasks":
            return {"board": board, "table": table, "uid": uid, "state": "live", "row": copy.deepcopy(self.task)}
        if self.audit and uid == self.audit["uid"]:
            row = {**self.audit, **({"payload": self.audit_payload} if self.audit_payload else {})}
            return {"board": board, "table": table, "uid": uid, "state": "live", "row": copy.deepcopy(row)}
        result = self.source.row_evidence(board, table, uid)
        if self.no_proof and result["row"]["kind"] == "containment_closed":
            result["containment_v1"] = None
        return result


def setup(conn, monkeypatch):
    from tests.hermes_cli import test_kanban_containment_replica as original
    hub = ArchiveHub(conn)
    original.apply(conn, hub.source, [original.event(hub.stop, hub.source, 1)])
    conn.execute("UPDATE kanban_sync_state SET mode='primary'")
    monkeypatch.setattr(client, "load_identity", lambda: hub.ident)
    monkeypatch.setattr(client, "Hub", lambda ident: hub)
    return hub


def test_archive_posts_normal_operation_and_projects_attested_closure(synced_conn, monkeypatch):
    hub = setup(synced_conn, monkeypatch)
    assert kb.archive_task(synced_conn, hub.target)
    assert hub.closed
    assert len(hub.posts) == 1
    assert hub.posts[0][0].keys() == {"op", "task", "client_op_id"}
    assert kb.get_task(synced_conn, hub.target).status == "archived"
    assert kb.get_task(synced_conn, hub.child).status == "todo"
    assert synced_conn.execute("SELECT COUNT(*) FROM containment_canonical_receipts WHERE kind='containment_closed'").fetchone()[0] == 1
    assert sync.outbox(synced_conn) == []


@pytest.mark.parametrize("capability", [None, True, 0, 2])
def test_missing_terminal_capability_refuses_before_local_write(synced_conn, monkeypatch, capability):
    hub = setup(synced_conn, monkeypatch)
    hub.capability["terminal_archive"] = capability
    before = synced_conn.total_changes
    with pytest.raises(ContainmentRefused, match="terminal archive"):
        kb.archive_task(synced_conn, hub.target)
    assert synced_conn.total_changes == before
    assert hub.posts == [] and hub.closed


def test_unknown_archive_recovers_same_request(synced_conn, monkeypatch):
    hub = setup(synced_conn, monkeypatch)
    hub.fail = True
    with pytest.raises(TimeoutError):
        kb.archive_task(synced_conn, hub.target)
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    hub.fail = False
    assert kb.archive_task(synced_conn, hub.target)
    assert hub.posts[0] == hub.posts[1]


def test_missing_canonical_closure_does_not_mutate_local_task(synced_conn, monkeypatch):
    hub = setup(synced_conn, monkeypatch)
    hub.no_proof = True
    with pytest.raises(ContainmentRefused):
        kb.archive_task(synced_conn, hub.target)
    assert kb.get_task(synced_conn, hub.target).status == "triage"


# --- exact audit metadata, canonical audit projection, creator scope ---------------------------------------------

def _audit_rows(conn, task):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='archived'", (task,))]


def test_metadata_is_persisted_sent_and_canonical_audit_event_is_projected(synced_conn, monkeypatch):
    hub = setup(synced_conn, monkeypatch)
    extra = {"operator_request_uid": "req-1", "n": {"a": [1, 2]}, "source_status": "ready", "via": "spoof"}
    assert kb.archive_task(synced_conn, hub.target, via="creator archive operator call", event_extra=extra)
    assert hub.posts[0][0]["archive_metadata"] == {"via": "creator archive operator call", "event_extra": extra}
    audit = _audit_rows(synced_conn, hub.target)
    assert audit == [{"operator_request_uid": "req-1", "n": {"a": [1, 2]}, "via": "creator archive operator call",
                      "source_status": "triage"}]
    assert synced_conn.execute("SELECT 1 FROM task_events WHERE uid=?", (hub.audit["uid"],)).fetchone()
    assert hub.snapshots == 0


def test_retry_with_changed_metadata_is_refused_before_network(synced_conn, monkeypatch):
    hub = setup(synced_conn, monkeypatch)
    hub.fail = True
    with pytest.raises(TimeoutError):
        kb.archive_task(synced_conn, hub.target, via="one", event_extra={"k": 1})
    hub.fail = False
    for kwargs in ({"via": "two", "event_extra": {"k": 1}}, {"via": "one", "event_extra": {"k": 2}}, {}):
        with pytest.raises(ContainmentRefused, match="different metadata"):
            kb.archive_task(synced_conn, hub.target, **kwargs)
    assert len(hub.posts) == 1
    assert kb.archive_task(synced_conn, hub.target, via="one", event_extra={"k": 1})  # exact same metadata recovers
    assert hub.posts[0] == hub.posts[1]


@pytest.mark.parametrize("tamper", ["payload", "missing", "kind", "task"])
def test_audit_mismatch_fails_closed_without_local_synthesis(synced_conn, monkeypatch, tamper):
    hub = setup(synced_conn, monkeypatch)
    real_ops = hub.ops

    def ops(board, o):
        out = real_ops(board, o)
        if tamper == "payload":
            hub.audit_payload = json.dumps({"source_status": "triage", "extra": "forged"})
        elif tamper == "kind":
            hub.audit["kind"] = "comment"
        elif tamper == "task":
            hub.audit["task_id"] = hub.child
        return out
    hub.ops = ops
    orig = hub.containment_context
    if tamper == "missing":
        hub.containment_context = lambda *a, **k: {**orig(*a, **k), "archive_event": None}
    with pytest.raises(ContainmentRefused):
        kb.archive_task(synced_conn, hub.target, via="v", event_extra={"k": 1})
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    assert _audit_rows(synced_conn, hub.target) == []


@pytest.mark.parametrize("extra,via", [({"x": "y" * 20000}, None), ({"a": float("nan")}, None), (None, "v" * 2001),
                                      ({"a": {"b": 1}}, 5)])
def test_unbounded_metadata_refused_before_network(synced_conn, monkeypatch, extra, via):
    hub = setup(synced_conn, monkeypatch)
    with pytest.raises(ContainmentRefused):
        kb.archive_task(synced_conn, hub.target, via=via, event_extra=extra)
    assert hub.posts == [] and not hub.closed


def test_generic_local_precondition_refused_before_any_network(synced_conn, monkeypatch):
    hub = setup(synced_conn, monkeypatch)
    calls = []
    monkeypatch.setattr(client, "Hub", lambda ident: calls.append(ident) or hub)
    with pytest.raises(ContainmentRefused, match="precondition"):
        kb.archive_task(synced_conn, hub.target, precondition=lambda c: None)
    assert calls == [] and hub.posts == [] and not hub.closed


def _creator(conn, hub, creator):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload=json_set(COALESCE(payload,'{}'),'$.creator_task_id',?) "
                     "WHERE task_id=? AND kind='created'", (creator, hub.target))


def _call(requester, target, uid="op-uid-1"):
    return {"request_uid": uid, "target_task_id": target, "action": "creator_archive",
            "caller": {"gateway": "worker", "task_id": requester, "run_id": 1, "run_uid": "ru"}, "args": {}}


def test_creator_archive_operator_handler_uses_canonical_route_and_settles(synced_conn, monkeypatch):
    """Real operator apply handler -> real archive_task -> Board op -> exact audit projection -> receipt settles."""
    from hermes_cli import kanban_operator_call as oc
    hub = setup(synced_conn, monkeypatch)
    requester = kb.create_task(synced_conn, title="creator", assignee="default")
    _creator(synced_conn, hub, requester)
    out = oc._apply(synced_conn, _call(requester, hub.target), "worker")
    assert out == ("applied", None)
    op = hub.posts[0][0]
    assert op["archive_metadata"]["via"] == "creator archive operator call"
    assert op["archive_metadata"]["event_extra"]["operator_request_uid"] == "op-uid-1"
    assert kb.get_task(synced_conn, hub.target).status == "archived"
    assert oc._marker(synced_conn, hub.target, "archived", "op-uid-1")  # the operator's own receipt is the canonical event
    assert oc._apply(synced_conn, _call(requester, hub.target), "worker") == ("already_applied", None)
    assert len(hub.posts) == 1


def test_creator_archive_wrong_requester_refused_before_network(synced_conn, monkeypatch):
    from hermes_cli import kanban_operator_call as oc
    hub = setup(synced_conn, monkeypatch)
    creator = kb.create_task(synced_conn, title="creator", assignee="default")
    other = kb.create_task(synced_conn, title="other", assignee="default")
    _creator(synced_conn, hub, creator)
    out = oc._apply(synced_conn, _call(other, hub.target), "worker")
    assert out[0] == "refused" and hub.posts == [] and not hub.closed
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    # the scope type is exact: a lookalike predicate is a generic precondition and is refused
    from hermes_cli.kanban_creator_archive import CreatorScope
    with pytest.raises(ContainmentRefused):
        kb.archive_task(synced_conn, hub.target, precondition=lambda c: CreatorScope(hub.target, creator)(c))
    with pytest.raises(ContainmentRefused, match="creator_archive requires"):
        kb.archive_task(synced_conn, hub.target, precondition=CreatorScope(hub.target, other))
    assert hub.posts == []
