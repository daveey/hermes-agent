"""Freshness and independent-gate controls through actual CLI/socket/HTTP/HOME.

Shares the disposable loopback Board from test_owner_release_composed. No direct
canonical SQL, private authority injection or synthetic receipt construction.
"""
import json
import uuid

import pytest

from hermes_cli import kanban_origin
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_owner_release_composed as helpers
from tests.tools import test_kanban_worker_created_chats as chats

turn = helpers.turn
server = helpers.server
origin = helpers.origin
parties = helpers.parties


def put(party, table, uid, fields):
    result = party.hub.ops(helpers.BOARD, [{"client_op_id": str(uuid.uuid4()), "op": "upsert", "table": table,
        "uid": uid, "fields": fields, "hlc": helpers.stamp(party.name)}])["results"][0]
    assert result["status"] == "applied", result


def publish(native, task, episode):
    first = helpers.release_cli(native, task, episode)
    assert first.returncode == 0, (first.stdout, first.stderr)
    nonce = json.loads(first.stdout)["release_read"]["nonce"]
    ack = helpers.release_cli(native, task, episode, "--seen-release", nonce)
    assert ack.returncode == 0, (ack.stdout, ack.stderr)
    value = json.loads(ack.stdout)
    assert value["publication_stage"] == "published", value
    return value["intent_uid"]


def consume_wire(home, task, intent_uid):
    proof = home.hub.owner_release_evidence(helpers.BOARD, intent_uid)
    comments = home.hub.owner_release_comments(helpers.BOARD, intent_uid)["comments"]
    return {"client_op_id": str(uuid.uuid4()), "op": "consume_owner_release", "task": task,
            "intent_uid": intent_uid, "digest": proof["digest"], "seen_comments": comments}


def assert_consume_refusal(home, wire, reason):
    value = home.hub.ops(helpers.BOARD, [wire])["results"][0]
    assert value["status"] == "rejected" and value["reason"] == reason, value


def assert_held(home, executor, task, intent_uid, monkeypatch, *, state="authorized"):
    before = helpers.event_inventory(home, task)
    attempts = set()
    row = None
    for _ in range(128):
        helpers.home_tick(home, monkeypatch)
        row = home.conn.execute("SELECT * FROM owner_release_home_queue WHERE intent_uid=?", (intent_uid,)).fetchone()
        if row and row["checked"]:
            attempts.add(row["checked"])
        if len(attempts) >= 3:
            break
    assert row is not None and len(attempts) >= 3, "HOME never processed the exact queued request"
    assert home.hub.owner_release_evidence(helpers.BOARD, intent_uid)["state"] == state
    assert row["stage"] == {"authorized": "consume_pending", "consumed": "consumed_pending_apply"}[state]
    assert not helpers.applied(home, intent_uid)
    assert not helpers.release_receipts(home, task)
    after = helpers.event_inventory(home, task)
    if state == "consumed":
        consume = home.hub.owner_release_consumption(helpers.BOARD, intent_uid)["renewal"]
        assert [r[0] for r in after if r not in before] == [consume["uid"]]
        after = [r for r in after if r[0] != consume["uid"]]
    assert after == before
    assert decision.execution_restricted(home.conn, task)
    executor.resync()
    assert decision.execution_restricted(executor.conn, task)
    with pytest.raises(ksc.HubError):
        home.hub.owner_release_application(helpers.BOARD, intent_uid)


@pytest.mark.parametrize("mode", ["released", "ambiguous", "other-gateway"])
def test_real_owner_link_changed_after_publication_refuses_home(origin, parties, monkeypatch, mode):
    home, executor = parties
    ident, native = origin
    task, episode = helpers.seed_stopped_task(home, ident, chats.CHAT)
    intent_uid = publish(native, task, episode)
    wire = consume_wire(home, task, intent_uid)
    helpers.install_allowance(home, task, episode, intent_uid)
    home.pull()
    link = home.conn.execute("SELECT uid FROM task_chat_links WHERE task_id=? AND kind='created'", (task,)).fetchone()[0]
    actor = helpers.Party("actor-control", home.path.parent)
    try:
        owner = ksc.Hub(ident)
        try:
            if mode == "ambiguous":
                fields = {"task_id": task, "chat_gateway": ident.gateway, "chat_profile": "default",
                          "chat_session": "another-chat", "kind": "created", "released": 0, "created_at": 1000}
                uid = "conflicting-link-" + uuid.uuid4().hex
            else:
                fields = {"released": 1} if mode == "released" else {"chat_gateway": actor.name}
                uid = link
            result = owner.ops(helpers.BOARD, [{"client_op_id": str(uuid.uuid4()), "op": "upsert",
                "table": "task_chat_links", "uid": uid, "fields": fields,
                "hlc": helpers.stamp(ident.gateway)}])["results"][0]
            assert result["status"] == "applied", result
        finally:
            owner.close()
        assert_consume_refusal(home, wire, "owner_release_actor")
        assert_held(home, executor, task, intent_uid, monkeypatch)
    finally:
        actor.close()


def test_real_changed_comments_invalidate_emitted_read_nonce(origin, parties, monkeypatch):
    home, executor = parties
    ident, native = origin
    task, episode = helpers.seed_stopped_task(home, ident, chats.CHAT)
    first = helpers.release_cli(native, task, episode)
    assert first.returncode == 0, (first.stdout, first.stderr)
    nonce = json.loads(first.stdout)["release_read"]["nonce"]
    put(home, "task_comments", "comment-" + uuid.uuid4().hex,
        {"task_id": task, "author": "fixture", "body": "New current instruction, not acknowledged", "created_at": 1001})
    before = helpers.event_inventory(home, task)
    ack = helpers.release_cli(native, task, episode, "--seen-release", nonce)
    value = json.loads(ack.stdout)
    assert ack.returncode == 0 and value["ok"] is True, value
    assert value["release_stage"] == "read_first_pending", value
    assert value["release_read"]["nonce"] != nonce
    assert value.get("publication_stage") != "published", value
    assert value.get("release_stage") != "applied", value
    helpers.home_tick(home, monkeypatch, rounds=3)
    assert not helpers.release_receipts(home, task)
    assert helpers.event_inventory(home, task) == before
    executor.resync()
    assert decision.execution_restricted(executor.conn, task)


@pytest.mark.parametrize("mode", ["comments", "task", "dependency", "no-allowance"])
def test_real_postpublication_current_gates_refuse_before_effects(origin, parties, monkeypatch, caplog, mode):
    home, executor = parties
    ident, native = origin
    # This one control exercises the legacy signed-window v1 contract. The
    # ordinary v2 path deliberately needs no signer (covered by the v2 suite).
    if mode == "no-allowance":
        capability = ksc.Hub.decision_retain_capability
        monkeypatch.setattr(ksc.Hub, "decision_retain_capability",
                            lambda hub, board: {**capability(hub, board), "owner_release_application": 1})
    task, episode = helpers.seed_stopped_task(home, ident, chats.CHAT)
    intent_uid = publish(native, task, episode)
    wire = consume_wire(home, task, intent_uid)
    if mode != "no-allowance":
        helpers.install_allowance(home, task, episode, intent_uid)
    if mode == "comments":
        put(home, "task_comments", "late-" + uuid.uuid4().hex,
            {"task_id": task, "author": "fixture", "body": "Changed after emitted acknowledgement", "created_at": 1001})
    elif mode == "task":
        put(home, "tasks", task, {"title": "Changed original acceptance snapshot"})
    elif mode == "dependency":
        parent = "t_" + uuid.uuid4().hex[:16]
        put(home, "tasks", parent, {"title": "Independent unfinished prerequisite", "status": "todo", "assignee": "default", "created_at": 1000})
        put(home, "task_links", f"{parent}>{task}",
            {"parent_id": parent, "child_id": task})
    # No independent signed allowance is provisioned for no-allowance: added
    # budget cannot manufacture canonical grants from the original release intent.
    home.resync()
    if mode in {"comments", "task"}:
        assert_consume_refusal(home, wire, {"comments": "owner_release_read_first", "task": "owner_release_changed"}[mode])
    replies = []
    actual_ops = ksc.Hub.ops

    def observe_ops(hub, board, ops, *args, **kwargs):
        result = actual_ops(hub, board, ops, *args, **kwargs)
        if any(op.get("op") == "apply_owner_release" and op.get("intent_uid") == intent_uid for op in ops):
            replies.extend(result["results"])
        return result

    monkeypatch.setattr(ksc.Hub, "ops", observe_ops)
    assert_held(home, executor, task, intent_uid, monkeypatch,
                state="consumed" if mode in {"dependency", "no-allowance"} else "authorized")
    if mode == "dependency":
        delivery = home.conn.execute("SELECT delivery_uid FROM owner_release_home_queue WHERE intent_uid=?", (intent_uid,)).fetchone()[0]
        assert any(delivery in r.getMessage() and "HOME dependencies remain unsatisfied" in r.getMessage()
                   for r in caplog.records)
    if mode == "no-allowance":
        from hermes_cli import kanban_release_final as final
        frozen = final.saved(home.conn, intent_uid)
        assert frozen is not None
        assert replies and replies[0]["status"] == "rejected" and replies[0]["reason"] == "owner_release_allowance", replies
        value = home.hub.ops(helpers.BOARD, [frozen])["results"][0]
        assert value["status"] == "rejected" and value["reason"] == "owner_release_conflict", value


def test_real_stale_episode_cannot_authorize_current_scope(origin, parties, monkeypatch):
    home, executor = parties
    ident, native = origin
    task, current, old = helpers.seed_stacked_task(home, ident, chats.CHAT)
    before = helpers.event_inventory(home, task)
    stale = helpers.release_cli(native, task, old)
    value = json.loads(stale.stdout)
    assert stale.returncode == 2 and not value["ok"], value
    assert value.get("publication_stage") != "published", value
    helpers.home_tick(home, monkeypatch, rounds=3)
    assert not helpers.release_receipts(home, task)
    assert helpers.event_inventory(home, task) == before
    assert current != old
    executor.resync()
    assert decision.execution_restricted(executor.conn, task)


@pytest.mark.parametrize("safety", [False, True], ids=["scope-successor", "safety-successor"])
def test_real_historical_native_replay_never_renews_successor(origin, parties, monkeypatch, safety):
    home, executor, native, task, episode, intent_uid, old = helpers.authorize_and_drive_home(
        origin, parties, monkeypatch, stacked=True)
    certificate = home.hub.owner_release_application(helpers.BOARD, intent_uid)
    with monkeypatch.context() as scoped:
        scoped.setattr(kanban_origin, "gateway_names", lambda: (home.name, {home.name}))
        _, successor = helpers._home_stop(home, task=task, refused=safety)
    stop = home.hub.row_evidence(helpers.BOARD, "task_events", successor)["row"]
    assert json.loads(stop["payload"])["cause"] == ("safety_refusal" if safety else "scope_stopped")
    assert successor not in {old, episode}
    assert decision.execution_restricted(home.conn, task)
    before = helpers.event_inventory(home, task)
    task_before = home.hub.row_evidence(helpers.BOARD, "tasks", task)["row"]
    for _ in range(2):
        replay = helpers.release_cli(native, task, episode, "--capability-episode", old)
        value = json.loads(replay.stdout)
        assert replay.returncode == 0 and value["intent_uid"] == intent_uid, value
        assert value["release_stage"] == "applied" and value["execution_clearance"] is False, value
        helpers.home_tick(home, monkeypatch, rounds=3)
    assert home.hub.owner_release_application(helpers.BOARD, intent_uid) == certificate
    assert helpers.event_inventory(home, task) == before
    assert home.hub.row_evidence(helpers.BOARD, "tasks", task)["row"] == task_before
    assert successor not in [r["episode"] for r in helpers.release_receipts(home, task)]
    executor.resync()
    assert decision.execution_restricted(executor.conn, task)
