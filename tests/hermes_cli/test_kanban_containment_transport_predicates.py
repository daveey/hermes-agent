"""Adjacent transport predicate controls; all Hub responses are explicit doubles."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_containment_canonical as canonical
from hermes_cli import kanban_containment_remote as remote
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_containment_actor import Actor
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_canonical as wire

board = wire.board


def context_data():
    return {"version": 1, "board": "fixture", "target": {"board": "fixture", "gateway": "home", "task_id": wire.TASK}, "held": False}


@pytest.mark.parametrize("fault", ["version-type", "version", "board", "target-board", "target-task", "held"])
def test_context_checks_each_coordinate(fault):
    data = context_data()
    hub = SimpleNamespace(containment_context=lambda *a: data)
    assert remote.context(hub, "fixture", wire.TASK) == data
    changes = {
        "version-type": lambda: data.update(version=True),
        "version": lambda: data.update(version=2),
        "board": lambda: data.update(board="other"),
        "target-board": lambda: data["target"].update(board="other"),
        "target-task": lambda: data["target"].update(task_id=wire.AUTH),
        "held": lambda: data.update(held=0),
    }
    changes[fault]()
    message = "target changed" if fault in ("target-board", "target-task", "held") else "capability is absent"
    with pytest.raises(ContainmentRefused, match=message):
        remote.context(hub, "fixture", wire.TASK)


@pytest.mark.parametrize("fault", ["absent", "mode", "gateway"])
def test_enrolled_requires_primary_and_same_gateway(monkeypatch, fault):
    from hermes_cli import kanban_sync_client as client
    state = {"mode": "primary", "board_id": "fixture", "gateway_id": "worker"}
    closed = []
    hub = SimpleNamespace(ident=SimpleNamespace(gateway="worker"), close=lambda: closed.append(True))
    monkeypatch.setattr(sync, "sync_state", lambda c: state)
    monkeypatch.setattr(client, "load_identity", lambda: None)
    monkeypatch.setattr(client, "Hub", lambda ident: hub)
    monkeypatch.setattr(remote, "require_capability", lambda *a: None)
    assert remote.enrolled(None) == (hub, "fixture")
    if fault == "absent":
        state.clear()
    else:
        state["mode" if fault == "mode" else "gateway_id"] = "shadow" if fault == "mode" else "other"
    message = "enrolled gateway differs" if fault == "gateway" else "attached primary board"
    with pytest.raises(ContainmentRefused, match=message):
        remote.enrolled(None)
    assert closed == ([True] if fault == "gateway" else [])


def test_replay_refuses_two_matching_unresolved_requests(board):
    op, _, _ = wire.operation()
    actor = Actor(op["actor_task"], op["actor_run_uid"])
    first = canonical.stage(board, board="fixture", gateway="worker", op=op)
    assert remote._replay(board, "fixture", actor, op["task"], "contain") == first
    second = {**op, "client_op_id": sync.uuid7(), "event_uid": sync.uuid7()}
    canonical.stage(board, board="fixture", gateway="worker", op=second)
    with pytest.raises(ContainmentRefused, match="ambiguous containment request history"):
        remote._replay(board, "fixture", actor, op["task"], "contain")


@pytest.mark.parametrize("fault", ["request", "event", "op", "gateway", "actor", "payload"])
def test_stage_requires_portable_and_allowed_coordinates(board, fault):
    op, _, _ = wire.operation()
    gateway = "worker"
    canonical.stage(board, board="fixture", gateway=gateway, op=op)
    op = copy.deepcopy(op)
    if fault == "gateway":
        gateway = ""
    else:
        key = {"request": "client_op_id", "event": "event_uid", "op": "op", "actor": "actor_run_uid", "payload": "payload"}[fault]
        op[key] = [] if fault == "payload" else "invalid"
    message = "lacks actor or predicate" if fault in ("actor", "payload") else "requires portable request/event identities"
    with pytest.raises(ContainmentRefused, match=message):
        canonical.stage(board, board="fixture", gateway=gateway, op=op)


@pytest.mark.parametrize("fault", ["table", "uid", "task", "kind", "deleted", "row-type"])
def test_canonical_event_requires_exact_row_identity(fault):
    op, stop, episode = wire.operation()
    proof = wire.evidence(op, stop, episode)
    args = dict(board="fixture", uid=episode, task=op["task"], kind="contained")
    assert canonical.canonical_event(proof, **args)
    if fault == "table":
        proof["table"] = "tasks"
    elif fault == "row-type":
        proof["row"] = []
    else:
        key = {"uid": "uid", "task": "task_id", "kind": "kind", "deleted": "deleted_at"}[fault]
        proof["row"][key] = 1 if fault == "deleted" else "invalid"
    message = "identity is unproven" if fault == "table" else "missing or inconsistent"
    with pytest.raises(ContainmentRefused, match=message):
        canonical.canonical_event(proof, **args)


def test_rejected_request_is_never_reposted(board):
    request, op, hub = wire.staged(board)
    hub.reply["results"][0].update(status="rejected", reason="fixture")
    with pytest.raises(ContainmentRefused, match="Board refused containment"):
        canonical.deliver(board, hub, request=request)
    assert hub.posts == [[op]]
    with pytest.raises(ContainmentRefused, match="previously refused this exact"):
        canonical.deliver(board, hub, request=request)
    assert hub.posts == [[op]]


@pytest.mark.parametrize("fault", ["episode", "kind"])
def test_resume_replay_cannot_change_release_identity(board, monkeypatch, fault):
    op, _stop, episode = wire.operation("resume_contained")
    request = canonical.stage(board, board="fixture", gateway="worker", op=op)
    actor = Actor(op["actor_task"], op["actor_run_uid"])
    closed = []
    hub = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(remote, "enrolled", lambda c: (hub, "fixture"))
    monkeypatch.setattr(remote, "_recover_stop", lambda *a: None)
    calls = []
    monkeypatch.setattr(remote, "_deliver", lambda *a: calls.append(a) or {"fixture": True})
    monkeypatch.setattr(remote, "settle_local", lambda *a: True)
    monkeypatch.setattr(remote, "context", lambda *a: {"status": "ready"})
    assert remote.resume(board, actor=actor, task=op["task"], episode=episode, kind="authority")["status"] == "ready"
    assert len(calls) == 1 and calls[0][-1] == request
    with pytest.raises(ContainmentRefused, match="cannot replace the exact release episode or predicate"):
        remote.resume(board, actor=actor, task=op["task"],
                      episode=sync.uuid7() if fault == "episode" else episode,
                      kind="producer" if fault == "kind" else "authority")
    assert len(calls) == 1
    assert len(closed) == 2


@pytest.mark.parametrize("fault", ["uid", "kind"])
def test_resume_rejects_bad_input_before_transport(monkeypatch, fault):
    calls = []
    monkeypatch.setattr(remote, "enrolled", lambda c: calls.append(c))
    with pytest.raises(ContainmentRefused, match="resume requires exact episode"):
        remote.resume(None, actor=Actor(wire.AUTH, sync.uuid7()), task=wire.TASK,
                      episode="invalid" if fault == "uid" else sync.uuid7(),
                      kind="invalid" if fault == "kind" else "authority")
    assert calls == []


@pytest.mark.parametrize("fault", ["episode", "hash", "shape"])
def test_certified_release_rederives_episode_and_payload(fault):
    import json
    op, stop, episode = wire.operation("resume_contained")
    hub = wire.Hub(op, stop, episode)
    original = hub.events[episode]
    proof = hub.events[op["event_uid"]]
    args = dict(board="fixture", task=op["task"], episode=episode, receipt=op["event_uid"])
    assert canonical.certified_release(original, proof, **args)
    if fault == "shape":
        payload = json.loads(proof["row"]["payload"])
        payload["evidence"]["run_uid"] = "invalid"
        proof["row"]["payload"] = json.dumps(payload)
    else:
        proof["containment_v1"]["episode" if fault == "episode" else "containment_sha256"] = sync.uuid7()
    with pytest.raises(ContainmentRefused, match="not bound to this exact containment episode"):
        canonical.certified_release(original, proof, **args)



@pytest.mark.parametrize("fault", ["missing-producer", "baseline-type", "event", "run"])
def test_resume_producer_requires_nomination_and_portable_completion(board, monkeypatch, fault):
    op, frozen, episode = wire.operation()
    frozen["policy"]["producer"] = {**frozen["target"], "task_id": wire.AUTH}
    hub = wire.Hub(op, frozen, episode)
    hub.close = lambda: None
    data = context_data()
    data["target"]["task_id"] = wire.AUTH
    data["producer_baseline"] = {"event_uid": sync.uuid7(), "run_uid": sync.uuid7()}
    hub.containment_context = lambda *a: data
    monkeypatch.setattr(remote, "enrolled", lambda c: (hub, "fixture"))
    monkeypatch.setattr(remote, "_recover_stop", lambda *a: None)
    class ReachedStage(RuntimeError):
        pass
    def stage(*args):
        assert args[-1]["evidence"]["event_uid"] == data["producer_baseline"]["event_uid"]
        raise ReachedStage
    monkeypatch.setattr(remote, "_stage", stage)
    args = dict(actor=Actor(wire.AUTH, sync.uuid7()), task=wire.TASK, episode=episode, kind="producer")
    with pytest.raises(ReachedStage):
        remote.resume(board, **args)
    if fault == "missing-producer":
        del frozen["policy"]["producer"]
        hub.events[episode] = wire.evidence(op, frozen, episode)
    elif fault == "baseline-type":
        data["producer_baseline"] = []
    else:
        data["producer_baseline"]["event_uid" if fault == "event" else "run_uid"] = "invalid"
    message = "has no producer predicate" if fault == "missing-producer" else "has no portable completion evidence"
    with pytest.raises(ContainmentRefused, match=message):
        remote.resume(board, **args)


def test_settle_local_does_not_pick_up_absent_target(board):
    def forbidden(*args):
        pytest.fail("absent retained target must not fetch or apply a projection")
    hub = SimpleNamespace(row_evidence=forbidden)
    assert remote.settle_local(board, hub, "fixture", {"task_id": wire.TASK}, Actor(wire.AUTH, sync.uuid7())) is False


@pytest.mark.parametrize("fault", ["board", "table", "uid", "state", "row-type", "task-id", "run-task-id"])
def test_settle_local_rechecks_each_canonical_projection_coordinate(board, monkeypatch, fault):
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import kanban_containment_service as service
    from tests.hermes_cli import test_kanban_containment_remote as fixtures
    target, authority, session, hub = fixtures.setup(board, monkeypatch)
    run_uid = board.execute("SELECT uid FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (target,)).fetchone()[0]
    actor = Actor(target, run_uid)
    with scoped_current_session_id(session):
        result = service.contain(board, authority=authority, summary="Fixture")
    assert remote.settle_local(board, hub, "fixture", result, actor) is True
    original = hub.row_evidence
    def altered(slug, table, uid):
        proof = original(slug, table, uid)
        selected = "task_runs" if fault == "run-task-id" else "tasks"
        if table != selected:
            return proof
        if fault == "row-type":
            proof["row"] = []
        elif fault in ("task-id", "run-task-id"):
            proof["row"]["id" if table == "tasks" else "task_id"] = wire.TASK
        else:
            proof[fault] = "other"
        return proof
    monkeypatch.setattr(hub, "row_evidence", altered)
    with pytest.raises(ContainmentRefused, match="canonical lifecycle projection changed identity"):
        remote.settle_local(board, hub, "fixture", result, actor)
