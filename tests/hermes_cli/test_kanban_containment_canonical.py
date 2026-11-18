"""Canonical adapter: exact custody, unknown outcomes and durable replay.

The Hub double models the documented wire contract, not a live Board or replica
proof. Board's authenticated ingress tests exercise the producer of these bytes.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_containment_canonical as canonical
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_state import fingerprint
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_legacy_paths as legacy

board = legacy.board
TASK = "t_0123456789abcdef"
AUTH = "t_1123456789abcdef"


def operation(kind="contain"):
    target = {"gateway": "home", "board": "fixture", "task_id": TASK}
    stop = {"version": 1, "target": target, "policy": {"version": 1, "authority": {**target, "task_id": AUTH}},
            "producer_baseline": None}
    episode = uuid7()
    payload = stop if kind == "contain" else {
        "version": 1, "episode": episode, "containment_sha256": fingerprint(stop), "resume_status": "ready",
        "evidence": {"kind": "authority", "authority": stop["policy"]["authority"], "run_uid": uuid7()}}
    op = {"client_op_id": uuid7(), "op": kind, "task": TASK, "actor_task": AUTH,
          "actor_run_uid": uuid7(), "event_uid": episode if kind == "contain" else uuid7(), "payload": payload}
    return op, stop, episode


def evidence(op, stop, episode):
    return {"board": "fixture", "table": "task_events", "uid": op["event_uid"], "state": "live",
            "row": {"uid": op["event_uid"], "task_id": TASK,
                    "kind": "contained" if op["op"] == "contain" else "containment_released",
                    "payload": json.dumps(op["payload"], separators=(",", ":")), "origin_gateway": "worker"},
            "containment_v1": {"version": 1, "episode": episode, "containment_sha256": fingerprint(stop),
                               "gateway": "worker", "request": op["client_op_id"]}}


class Hub:
    def __init__(self, op, stop, episode):
        self.ident = SimpleNamespace(gateway="worker")
        self.posts = []
        self.op = op
        self.reply = {"results": [{"client_op_id": op["client_op_id"], "status": "applied"}]}
        self.events = {op["event_uid"]: evidence(op, stop, episode)}
        if op["op"] != "contain":
            original = {**op, "op": "contain", "event_uid": episode, "payload": stop, "client_op_id": uuid7()}
            self.events[episode] = evidence(original, stop, episode)
        self.fail = False

    def ops(self, slug, ops):
        assert slug == "fixture"
        self.posts.append(copy.deepcopy(ops))
        if self.fail:
            raise TimeoutError("fixture accepted request but reply was lost")
        return self.reply

    def row_evidence(self, slug, table, uid):
        assert (slug, table) == ("fixture", "task_events")
        return copy.deepcopy(self.events[uid])


def staged(board, kind="contain"):
    op, stop, episode = operation(kind)
    request = canonical.stage(board, board="fixture", gateway="worker", op=op)
    return request, op, Hub(op, stop, episode)


@pytest.mark.parametrize("kind", ["contain", "resume_contained"])
def test_canonical_success_never_writes_task_or_dependencies(board, kind):
    relay, consumer = legacy._relay(board)
    before = legacy._edges(board)
    request, op, hub = staged(board, kind)
    result = canonical.deliver(board, hub, request=request)
    assert result == {"task_id": TASK, "request_uid": request, "event_uid": op["event_uid"],
                      "canonical_confirmed": True}
    assert legacy._edges(board) == before
    assert legacy.kb.get_task(board, relay).status == "running"
    assert legacy.kb.get_task(board, consumer).status == "todo"
    assert board.execute("SELECT confirmed FROM containment_delivery").fetchone()[0] == 1
    # Confirmed recovery still reads canonical evidence; it does not re-submit.
    assert canonical.deliver(board, hub, request=request) == result
    assert hub.posts == [[op]]


def test_uncertain_delivery_recovers_identical_bytes_after_real_connection_restart(board):
    import sqlite3
    request, op, hub = staged(board)
    hub.fail = True
    with pytest.raises(TimeoutError):
        canonical.deliver(board, hub, request=request)
    path = board.execute("PRAGMA database_list").fetchone()[2]
    hub.fail = False
    with sqlite3.connect(path, isolation_level=None) as fresh:
        fresh.row_factory = sqlite3.Row
        canonical.deliver(fresh, hub, request=request)
    assert hub.posts == [[op], [op]]
    assert board.execute("SELECT COUNT(*) FROM containment_delivery").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["payload", "actor_run_uid", "task", "gateway", "board"])
def test_request_cannot_change_on_retry(board, field):
    request, op, _ = staged(board)
    kwargs = {"board": "fixture", "gateway": "worker", "op": copy.deepcopy(op)}
    if field in ("gateway", "board"):
        kwargs[field] = "other"
    elif field == "payload":
        kwargs["op"][field]["producer_baseline"] = {"run_uid": uuid7(), "event_uid": None}
    elif field == "actor_run_uid":
        kwargs["op"][field] = uuid7()
    else:
        kwargs["op"][field] = AUTH
    with pytest.raises(ContainmentRefused, match="different containment bytes"):
        canonical.stage(board, **kwargs)
    assert board.execute("SELECT request_uid FROM containment_delivery").fetchone()[0] == request


@pytest.mark.parametrize("case", ["missing", "many", "nonobject", "other_request", "pending", "rejected"])
def test_malformed_or_rejected_disposition_is_not_success(board, case):
    request, _, hub = staged(board)
    valid = hub.reply["results"][0]
    hub.reply = {
        "missing": {}, "many": {"results": [valid, valid]}, "nonobject": {"results": [None]},
        "other_request": {"results": [{**valid, "client_op_id": uuid7()}]},
        "pending": {"results": [{**valid, "status": "pending"}]},
        "rejected": {"results": [{**valid, "status": "rejected"}]},
    }[case]
    with pytest.raises((canonical.DeliveryUnknown, ContainmentRefused)):
        canonical.deliver(board, hub, request=request)
    assert board.execute("SELECT confirmed FROM containment_delivery").fetchone()[0] == (-1 if case == "rejected" else 0)


@pytest.mark.parametrize("path,value", [
    (("board",), "other"), (("table",), "task_comments"), (("uid",), "bad"), (("state",), "deleted"),
    (("row",), None), (("row", "uid"), "bad"), (("row", "task_id"), AUTH),
    (("row", "kind"), "completed"), (("row", "deleted_at"), 1), (("row", "payload"), {}),
    (("containment_v1",), None), (("containment_v1", "version"), True),
    (("containment_v1", "episode"), "bad"), (("containment_v1", "request"), "bad"),
    (("containment_v1", "gateway"), ""), (("row", "origin_gateway"), "foreign"),
    (("containment_v1", "containment_sha256"), "0" * 64),
])
def test_attestation_guards_fail_closed(board, path, value):
    request, op, hub = staged(board)
    item = hub.events[op["event_uid"]]
    for part in path[:-1]:
        item = item[part]
    item[path[-1]] = value
    with pytest.raises(ContainmentRefused):
        canonical.deliver(board, hub, request=request)
    assert board.execute("SELECT confirmed FROM containment_delivery").fetchone()[0] == 0


def test_certified_receipt_for_different_request_or_payload_is_not_delivery(board):
    request, op, hub = staged(board)
    hub.events[op["event_uid"]]["containment_v1"]["request"] = uuid7()
    with pytest.raises(canonical.DeliveryUnknown):
        canonical.deliver(board, hub, request=request)
    # A different, internally certified policy still is not OUR staged policy.
    item = hub.events[op["event_uid"]]
    item["containment_v1"]["request"] = request
    payload = json.loads(item["row"]["payload"])
    payload["policy"]["authority"]["gateway"] = "new-home"
    item["row"]["payload"] = json.dumps(payload)
    item["containment_v1"]["containment_sha256"] = fingerprint(payload)
    with pytest.raises(canonical.DeliveryUnknown):
        canonical.deliver(board, hub, request=request)


def test_confirmed_recovery_rechecks_canonical_evidence(board):
    request, op, hub = staged(board)
    canonical.deliver(board, hub, request=request)
    hub.events[op["event_uid"]]["containment_v1"] = None
    with pytest.raises(ContainmentRefused):
        canonical.deliver(board, hub, request=request)
    assert len(hub.posts) == 1


def test_network_is_outside_writer_and_other_gateway_cannot_replay(board):
    request, _, hub = staged(board)
    hub.ident.gateway = "other"
    with pytest.raises(ContainmentRefused, match="different enrolled gateway"):
        canonical.deliver(board, hub, request=request)
    hub.ident.gateway = "worker"
    from hermes_cli import kanban_db as kb
    with kb.write_txn(board), pytest.raises(ContainmentRefused, match="writer transaction"):
        canonical.deliver(board, hub, request=request)
    assert hub.posts == []
