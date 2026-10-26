"""Server-owned metadata is durable, bounded and isolated from clients."""
import hashlib
import json

import pytest

from hermes_state import SessionDB
from tui_gateway.session_start_requests import StartIntent, StartOwner, StartStore


@pytest.fixture
def store(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    yield StartStore(db)
    db.close()


OWNER = StartOwner("example-gateway", "default", "example:user")


def accept(store, metadata=None):
    return store.accept(owner=OWNER, namespace="example", request_id="request",
                        intent=StartIntent("Title", "Inspect only", metadata=metadata))


def test_metadata_durable_retry_conflict_and_compression(store):
    row = accept(store, {"runner_repair_id": "repair-example"})
    assert accept(store, {"runner_repair_id": "repair-example"}) == row
    with pytest.raises(ValueError, match="conflict"):
        accept(store, {"runner_repair_id": "different"})
    sid = row["stored_session_id"]
    store.db.end_session(sid, "compression")
    store.db.create_session("child-example", source="desktop", parent_session_id=sid)
    with SessionDB(store.db.db_path) as reopened:
        status = StartStore(reopened).status(OWNER, "example", "request")
    assert status["resolved_session_id"] == "child-example"
    assert status["metadata"] == {"runner_repair_id": "repair-example"}
    assert store.lookup(StartOwner("example-gateway", "default", "other"), "example", "request") is None


@pytest.mark.parametrize("bad", [[], "text", {"k": 1}, {1: "v"}, {"": "v"}, {"x" * 65: "v"},
                                 {"k": "x" * 1025}, {str(i): "v" for i in range(33)}])
def test_metadata_validation(bad):
    with pytest.raises(ValueError, match="metadata"):
        StartIntent("Title", "Inspect", metadata=bad)


def test_metadata_cannot_mutate_after_validation():
    supplied = {"runner_repair_id": "original"}
    intent = StartIntent("Title", "Inspect", metadata=supplied)
    supplied["runner_repair_id"] = "changed"
    assert intent.metadata["runner_repair_id"] == "original"
    with pytest.raises(TypeError):
        intent.metadata["runner_repair_id"] = "changed"


def test_empty_metadata_preserves_old_digest(store):
    row = accept(store)
    old_digest = hashlib.sha256(json.dumps(dict(title="Title", text="Inspect only", source="desktop"),
                                          sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    assert row["intent_digest"] == old_digest
    assert row["metadata"] == {}


def test_upgrade_existing_ledger_keeps_retry_identity(store):
    row = accept(store)
    store.db._execute_write(lambda conn: conn.execute(
        "ALTER TABLE session_start_requests DROP COLUMN metadata_json"))
    upgraded = StartStore(store.db)
    assert accept(upgraded) == row
    assert upgraded.status(OWNER, "example", "request")["metadata"] == {}


def test_metadata_atomic_rollback(store, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError("rollback")
    monkeypatch.setattr(store.db, "_bump_session_counters", fail)
    with pytest.raises(RuntimeError, match="rollback"):
        accept(store, {"runner_repair_id": "example"})
    assert store.lookup(OWNER, "example", "request") is None
    assert store.db._read_one("SELECT count(*) FROM sessions")[0] == 0
