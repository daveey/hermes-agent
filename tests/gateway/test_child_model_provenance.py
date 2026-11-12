"""Child model provenance is diagnostic data, never worker authority over lineage or routing."""
import contextlib
import json
import os
from functools import partial

import psutil
import pytest

import hermes_state_runtime as rt
from agent.runtime_session_child import reserve_child_store
from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from gateway.session_controls import _refusal_data
from gateway.session_worker_child import reserve_child_execution
from hermes_cli.process_identity import worker_birth
from tests.hermes_state import test_runtime_worker_children as children

ledger = children.ledger


@pytest.mark.parametrize("provenance", [
    None,
    "caller_pin",
    {"model_source": "made_up", "requested_model": "class:standard"},
    {"model_source": "caller_pin", "requested_model": 42},
    {"model_source": "caller_pin"},
    {"model_source": "caller_pin", "requested_model": "class:standard", "_delegate_from": "other"},
])
def test_reservation_refuses_malformed_or_authority_bearing_provenance(ledger, provenance):
    db, epoch = ledger
    handler = partial(reserve_child_execution, epoch=epoch, execution_id="worker",
                      identity={}, principal="local", limits=(2, 1), refused=None)
    with pytest.raises(rt.RuntimeStoreError, match="^invalid_params$"):
        rt.apply_worker_receipt(db, epoch=epoch, execution_id="worker", session_id="parent", generation=0,
                                sequence=1, operation="child.reserve",
                                payload={"secret": "synthetic-child-secret", "model_provenance": provenance},
                                handlers={"child.reserve": handler})
    with db._read_ctx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE source='subagent'").fetchone()[0] == 0


PROVENANCE = {"model_source": "caller_pin", "requested_model": "pinned-model"}


def test_a_later_reservation_cannot_reissue_another_secrets_pending_entry(tmp_path, monkeypatch):
    calls = []

    def rpc(method, **params):
        calls.append(dict(params["payload"]))
        failure = WorkerPersistenceError("invalid_params")
        failure.data = {"field": "model_provenance"}
        raise failure

    parent = RuntimeSessionStore(rpc, {"session_id": "parent"}, tmp_path / "outbox")
    secrets = iter(["first-synthetic-secret", "second-synthetic-secret"])
    monkeypatch.setattr("agent.runtime_session_child.secrets.token_urlsafe", lambda _: next(secrets))
    try:
        with pytest.raises(WorkerPersistenceError, match="^invalid_params$"):
            reserve_child_store(parent, model_provenance=PROVENANCE)
        before = parent.path.read_bytes()
        with pytest.raises(WorkerPersistenceError, match="^invalid_params$"):
            reserve_child_store(parent, model_provenance=PROVENANCE)
        assert calls == [{"secret": "first-synthetic-secret", "model_provenance": PROVENANCE}]
        assert parent.path.read_bytes() == before
    finally:
        parent._outbox_owner.close()


def test_restart_replays_a_provenance_reservation_against_a_strict_old_owner(ledger, tmp_path):
    db, epoch = ledger
    calls = []
    parent = _worker_parent(db, epoch, tmp_path, old_owner=False, calls=calls)
    payload = {"secret": "replay-synthetic-secret", "model_provenance": PROVENANCE}
    # Crash boundary: durably journaled, but no owner response or live fallback yet.
    entry = {"sequence": 1, "operation": "child.reserve", "payload": payload}
    parent._save(dict(parent.journal, next_sequence=2, pending=[entry]))
    parent._outbox_owner.close()
    replayed = _worker_parent(db, epoch, tmp_path, old_owner=True, calls=calls)
    try:
        assert calls == [payload, {"secret": payload["secret"]}]
        journal = json.loads(replayed.path.read_text())
        assert journal["pending"] == []
        assert journal["acked"][0] == dict(entry, payload={"secret": payload["secret"]})
        with db._read_ctx() as conn:
            assert conn.execute("SELECT COUNT(*) FROM sessions WHERE source='subagent'").fetchone()[0] == 1
    finally:
        replayed.close()


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
def test_dropped_provenance_warns_once_without_secrets(tmp_path, caplog, replay):
    calls = []
    secret = "warning-synthetic-secret"

    def rpc(method, **params):
        calls.append(dict(params["payload"]))
        if "model_provenance" in params["payload"]:
            raise WorkerPersistenceError("invalid_params")
        return {"refused": "delegation_capacity"}

    parent = RuntimeSessionStore(rpc, {"session_id": "parent"}, tmp_path / "outbox")
    payload = {"secret": secret, "model_provenance": PROVENANCE}
    try:
        with caplog.at_level("WARNING", logger="agent.runtime_session_store"):
            if replay:
                entry = {"sequence": 1, "operation": "child.reserve", "payload": payload}
                parent._save(dict(parent.journal, next_sequence=2, pending=[entry]))
                parent._outbox_owner.close()
                parent = RuntimeSessionStore(rpc, parent.scope, parent.path.parent)
            else:
                parent._apply("child.reserve", payload)
        warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "child.reserve" in warnings[0]
        assert "provenance" in warnings[0] and "dropped" in warnings[0] and "owner refused" in warnings[0]
        assert secret not in warnings[0]
        assert [set(c) for c in calls] == [{"secret", "model_provenance"}, {"secret"}]
    finally:
        parent._outbox_owner.close()


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
def test_second_refusal_propagates_without_another_retry(tmp_path, caplog, replay):
    calls = []
    failures = [WorkerPersistenceError("invalid_params"), WorkerPersistenceError("invalid_params")]

    def rpc(method, **params):
        calls.append(dict(params["payload"]))
        assert len(calls) <= 2, "a stripped reservation must not be retried again"
        raise failures[len(calls) - 1]

    parent = RuntimeSessionStore(rpc, {"session_id": "parent"}, tmp_path / "outbox")
    try:
        if replay:
            entry = {"sequence": 1, "operation": "child.reserve",
                     "payload": {"secret": "second-refusal-secret", "model_provenance": PROVENANCE}}
            parent._save(dict(parent.journal, next_sequence=2, pending=[entry]))
            parent._outbox_owner.close()
        with caplog.at_level("WARNING", logger="agent.runtime_session_store"):
            with pytest.raises(WorkerPersistenceError, match="^invalid_params$") as caught:
                if replay:
                    RuntimeSessionStore(rpc, parent.scope, parent.path.parent)
                else:
                    reserve_child_store(parent, model_provenance=PROVENANCE)
        assert caught.value is failures[1]
        assert [set(c) for c in calls] == [{"secret", "model_provenance"}, {"secret"}]
        journal = json.loads(parent.path.read_text())
        assert journal["pending"][0]["payload"] == calls[1]
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
        # Another restart sends only the durable stripped entry, with no renewed fallback.
        parent._outbox_owner.close()
        calls.clear()
        with pytest.raises(WorkerPersistenceError, match="^invalid_params$"):
            RuntimeSessionStore(rpc, parent.scope, parent.path.parent)
        assert len(calls) == 1 and set(calls[0]) == {"secret"}
    finally:
        parent._outbox_owner.close()


@pytest.mark.parametrize("refusal,data,operation,provenance", [
    ("permission_denied", {}, "child.reserve", PROVENANCE),
    ("invalid_params", {"field": "model_provenance"}, "child.reserve", PROVENANCE),
    ("invalid_params", {}, "transcript.append", PROVENANCE),
    ("invalid_params", {}, "child.reserve", None),
], ids=["other-reason", "specific-field", "other-operation", "no-provenance"])
def test_unrelated_replay_refusal_is_not_stripped(tmp_path, refusal, data, operation, provenance, caplog):
    calls = []
    failure = WorkerPersistenceError(refusal)
    failure.data = data

    def rpc(method, **params):
        calls.append(dict(params["payload"]))
        raise failure

    parent = RuntimeSessionStore(rpc, {"session_id": "parent"}, tmp_path / "outbox")
    payload = {"secret": "control-synthetic-secret"}
    if provenance is not None:
        payload["model_provenance"] = provenance
    entry = {"sequence": 1, "operation": operation, "payload": payload}
    parent._save(dict(parent.journal, next_sequence=2, pending=[entry]))
    parent._outbox_owner.close()
    before = parent.path.read_bytes()
    with pytest.raises(WorkerPersistenceError, match=f"^{refusal}$") as caught:
        RuntimeSessionStore(rpc, parent.scope, parent.path.parent)
    assert caught.value is failure
    assert calls == [payload]
    assert parent.path.read_bytes() == before
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def _worker_parent(db, epoch, tmp_path, *, old_owner, calls):
    """A worker's parent store whose RPC reaches the real receipt transaction; ``old_owner`` swaps in the
    reservation check origin/main shipped (payload must be exactly ``{'secret'}``)."""
    reserve = partial(reserve_child_execution, epoch=epoch, execution_id="worker",
                      identity={"profile_id": "home", "pid": os.getpid(), "birth": worker_birth(psutil.Process())},
                      principal="local", limits=(2, 1), refused=None)

    def strict_old_owner(db_, conn, session_id, payload, **kwargs):
        if set(payload) != {"secret"}:
            raise rt.RuntimeStoreError("invalid_params")
        return reserve(db_, conn, session_id, payload, **kwargs)

    handler = strict_old_owner if old_owner else reserve

    def rpc(method, **params):
        fields = {k: params[k] for k in (
            "epoch", "execution_id", "session_id", "generation", "sequence", "operation", "payload")}
        calls.append(dict(fields["payload"]))
        try:
            if fields["operation"] == "child.reserve":
                return rt.apply_worker_receipt(db, **fields, handlers={"child.reserve": handler})
            return rt.mutate_worker_execution(db, **fields)
        except rt.RuntimeStoreError as exc:  # the wire: bare reason as the message, structure as error data
            failure = WorkerPersistenceError(exc.reason)
            failure.data = _refusal_data(exc)
            raise failure from exc

    return RuntimeSessionStore(rpc, dict(epoch=epoch, execution_id="worker", session_id="parent", generation=0),
                               tmp_path / "outbox")


def test_a_newer_worker_still_spawns_its_child_on_an_older_owner_that_rejects_provenance(ledger, tmp_path):
    """Version skew: the old owner accepts only ``{'secret'}`` and answers ``invalid_params``. The worker retries the
    reservation once without the provenance keys, so the child spawns (unrecorded) instead of failing."""
    db, epoch = ledger
    calls = []
    parent = _worker_parent(db, epoch, tmp_path, old_owner=True, calls=calls)
    try:
        child = reserve_child_store(parent, model_provenance=PROVENANCE)
        try:
            assert [set(c) for c in calls] == [{"secret", "model_provenance"}, {"secret"}]
            row = json.loads(db.get_session(child.reserved_session_id)["model_config"])
            assert row == {"_delegate_from": "parent"}  # spawned, unrecorded
            assert parent.failure is None  # the journal is not left latched for the parent's later writes
        finally:
            child.close()
    finally:
        parent.close()


@pytest.mark.parametrize("refusal,data", [
    ("permission_denied", {"reason": "permission_denied"}),
    ("invalid_params", {"reason": "invalid_params", "field": "sequence", "detail": "expected 1, got 2"}),
], ids=["other-reason", "other-invalid-params-field"])
def test_an_unrelated_reservation_refusal_is_not_retried_without_provenance(ledger, tmp_path, refusal, data):
    _, epoch = ledger
    calls = []

    def rpc(method, **params):
        calls.append(dict(params["payload"]))
        failure = WorkerPersistenceError(refusal)
        failure.data = data
        raise failure

    parent = RuntimeSessionStore(rpc, dict(epoch=epoch, execution_id="worker", session_id="parent", generation=0),
                                 tmp_path / "outbox")
    try:
        with pytest.raises(WorkerPersistenceError, match=f"^{refusal}$"):
            reserve_child_store(parent, model_provenance=PROVENANCE)
        assert [set(c) for c in calls] == [{"secret", "model_provenance"}]
    finally:
        with contextlib.suppress(WorkerPersistenceError):  # the refusal latched the journal, as it always has
            parent.close()
