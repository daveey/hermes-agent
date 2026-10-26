"""The origin acknowledges a durable obligation, never a replaceable replica."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_origin_ledger as ledger


@pytest.fixture
def world(tmp_path):
    replica = tmp_path / "hub.db"
    with sqlite3.connect(replica) as db:
        db.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, data TEXT)")
    binding = {
        "binding_id": "binding-0001", "version": 1,
        "owner": {"gateway": "owner", "profile": "work", "board": "build", "task_id": "t_one"},
        "origin": {"gateway": "origin", "profile": "default", "session": "s_old"},
        "task_record_key": "work/build/t_one", "publication_version": 3,
    }
    return tmp_path / "origin-work.db", replica, binding


def publish(replica, binding, **changes):
    task = {**binding["owner"], "id": binding["owner"]["task_id"], "status": "running",
            "origin_bindings": [binding], **changes}
    with sqlite3.connect(replica) as db:
        db.execute("DELETE FROM replica")
        db.execute("INSERT INTO replica VALUES (?, 'tasks', ?, ?, ?)",
                   (binding["owner"]["gateway"], binding["task_record_key"], 3, json.dumps(task)))


def accept(world, **changes):
    path, replica, binding = world
    args = dict(path=path, replica=replica, binding=binding, request_id="request-0001",
                sender="gw:owner", gateway="origin", profile="default",
                resolve_session=lambda sid: "s_current")
    args.update(changes)
    return ledger.accept_binding(**args)


@pytest.mark.parametrize("change", [
    {"sender": "gw:impostor"}, {"gateway": "elsewhere"}, {"profile": "work"},
])
def test_wrong_authenticated_route_cannot_accept_or_replay(world, change):
    path, replica, binding = world
    publish(replica, binding)
    with pytest.raises(ValueError, match="origin_route_mismatch"):
        accept(world, **change)
    assert ledger.receipt(path, "request-0001") is None
    accept(world)
    with pytest.raises(ValueError, match="origin_route_mismatch"):
        accept(world, **change)


@pytest.mark.parametrize("field,value", [
    ("version", 0), ("version", True), ("publication_version", -1),
    ("publication_version", "3"), ("binding_id", ""),
    ("task_record_key", "default/build/t_one"),
    ("owner", {"gateway": "owner", "board": "build", "task_id": "t_one"}),
    ("origin", {"gateway": "origin", "profile": "../work", "session": "s_old"}),
])
def test_invalid_binding_is_rejected_before_storage(world, field, value):
    from copy import deepcopy
    binding = deepcopy(world[2])
    binding[field] = value
    publish(world[1], binding)
    with pytest.raises(ValueError, match="origin_binding_invalid"):
        accept(world, binding=binding)
    assert not world[0].exists()


def test_replay_conflict_and_concurrent_accept_are_atomic(world):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    path, replica, binding = world
    publish(replica, binding)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: accept(world), range(8)))
    assert all(r == replies[0] for r in replies)
    altered = deepcopy(binding)
    altered["origin"]["session"] = "s_other"
    with pytest.raises(ValueError, match="origin_binding_conflict"):
        accept(world, binding=altered)
    # New request ID for the same binding is idempotent, not a duplicate obligation.
    assert accept(world, request_id="request-0002")["binding_hash"] == replies[0]["binding_hash"]
    altered["binding_id"] = binding["binding_id"]
    publish(replica, altered)
    with pytest.raises(ValueError, match="origin_binding_conflict"):
        accept(world, binding=altered, request_id="request-0003")
    assert ledger.receipt(path, "request-0003") is None
    assert len(ledger.obligations(path, gateway="origin", profile="default", sessions=["s_old"])) == 1


def test_receipt_cannot_outlive_its_obligation(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM origin_obligations")
    with pytest.raises(ValueError, match="origin_obligation_missing"):
        accept(world)


@pytest.mark.parametrize("change", [{"profile": "other"}, {"id": "t_other"}, {"board": "other"}])
def test_exact_task_identity_must_match_even_with_binding_marker(world, change):
    publish(world[1], world[2], **change)
    with pytest.raises(ValueError, match="origin_publication_mismatch"):
        accept(world)
    assert ledger.receipt(world[0], "request-0001") is None


def test_missing_session_has_no_receipt(world):
    publish(world[1], world[2])
    with pytest.raises(ValueError, match="origin_session_missing"):
        accept(world, resolve_session=lambda sid: None)
    assert ledger.receipt(world[0], "request-0001") is None


@pytest.mark.parametrize("request_id", [None, "", 123, "x" * 129])
def test_bad_request_id_cannot_write(world, request_id):
    publish(world[1], world[2])
    with pytest.raises(ValueError, match="origin_request_invalid"):
        accept(world, request_id=request_id)
    assert not world[0].exists()


def test_duplicate_binding_receipt_describes_persisted_projection(world):
    publish(world[1], world[2])
    first = accept(world)
    second = accept(world, request_id="request-0002", resolve_session=lambda sid: "s_new")
    assert second["resolved_session"] == first["resolved_session"]
    assert len(ledger.obligations(world[0], gateway="origin", profile="default",
                                  sessions=[second["resolved_session"]])) == 1


def test_different_owners_can_reuse_binding_id(world):
    from copy import deepcopy
    publish(world[1], world[2])
    accept(world)
    other = deepcopy(world[2])
    other["owner"]["gateway"] = "other"
    publish(world[1], other)
    accept(world, binding=other, sender="gw:other", request_id="request-0002")
    assert len(ledger.obligations(world[0], gateway="origin", profile="default", sessions=["s_old"])) == 2


def test_publication_marker_does_not_need_future_server_version(world):
    _path, replica, binding = world
    marker = {k: v for k, v in binding.items() if k != "publication_version"}
    publish(replica, binding, origin_bindings=[marker])
    assert accept(world)["status"] == "accepted"


def test_receipt_requires_publication_and_survives_replica_loss(world):
    path, replica, binding = world
    assert accept(world) == {"status": "pending", "code": "origin_publication_pending"}
    publish(replica, binding)
    receipt = accept(world)
    assert receipt["status"] == "accepted"
    assert receipt["binding_hash"] == ledger.binding_hash(binding)
    replica.unlink()
    # A different invocation/process reads durable acknowledgement and obligation.
    assert accept(world) == receipt
    rows = ledger.obligations(path, gateway="origin", profile="default", sessions=["s_current"])
    assert len(rows) == 1
    assert rows[0]["owner"] == binding["owner"]
    assert rows[0]["unfinished"] is True
    assert ledger.receipt(path, "request-0001") == receipt
