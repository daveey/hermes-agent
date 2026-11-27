"""Current independent safety/containment through native CLI and real Board HTTP.

No private Board injection: the safety source uses a real bound run/refusal retain;
containment uses its authenticated lease-bound ingress and frozen authority policy.
"""
import json
import uuid

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_origin
from tests.hermes_cli import test_owner_release_composed as helpers
from tests.hermes_cli import test_owner_release_composed_controls as controls
from tests.tools import test_kanban_worker_created_chats as chats

turn = helpers.turn
server = helpers.server
origin = helpers.origin
parties = helpers.parties


def _contain(home, task):
    """Real canonical self-containment from a live HOME lease, not a public lookalike."""
    authority = kb.create_task(home.conn, title="Independent containment decision", assignee="default")
    pushed = helpers.ksc.push(home.conn, home.hub, helpers.BOARD, home.name, primary=True)
    assert pushed.rejected == 0, pushed
    with kbc.write_txn(home.conn):
        rid = home.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at) VALUES (?, 'default', 'running', 1)",
                                (task,)).lastrowid
        home.conn.execute("UPDATE tasks SET status='running', current_run_id=? WHERE id=?", (rid, task))
        home.conn.execute("DELETE FROM kanban_sync_outbox")  # canonical lease supplies claim/run, as _home_stop
    run = sync.run_uid(home.conn, rid)
    grant = home.hub.leases(helpers.BOARD, {"action": "grant", "task": task, "run_uid": run, "ttl_s": 60})
    assert grant["status"] == "applied", grant
    context = home.hub.containment_context(helpers.BOARD, task)
    episode = str(uuid.uuid7())
    policy = {"version": 1, "authority": {"board": helpers.BOARD, "gateway": home.name, "task_id": authority}}
    payload = {"version": 1, "target": context["target"], "policy": policy, "producer_baseline": None}
    op = {"client_op_id": str(uuid.uuid7()), "op": "contain", "task": task, "actor_task": task,
          "actor_run_uid": run, "event_uid": episode, "payload": payload}
    result = home.hub.ops(helpers.BOARD, [op])["results"][0]
    assert result["status"] == "applied", result
    held = home.hub.containment_context(helpers.BOARD, task)
    assert held["held"] is True and held["containment_events"] == [episode], held
    event = home.hub.row_evidence(helpers.BOARD, "task_events", episode)["row"]
    assert event["origin_gateway"] == home.name and event["kind"] == "contained"
    return episode, held


@pytest.mark.parametrize("hold", ["safety", "containment"])
def test_real_current_independent_hold_refuses_native_release(origin, parties, monkeypatch, hold):
    home, executor = parties
    ident, native = origin
    with monkeypatch.context() as scoped:
        scoped.setattr(kanban_origin, "gateway_names", lambda: (home.name, {home.name}))
        task, episode = helpers._home_stop(home, reason="capability_unavailable" if hold == "containment" else "scope_stopped",
                                            refused=hold == "safety")
        helpers._link_origin(home, ident, chats.CHAT, task, episode)
        if hold == "containment":
            # Legacy capability promotion is not a release receipt. This real
            # next run contains itself; release must refuse the live containment
            # before interpreting the old capability as a scope episode. This is
            # ingress ordering coverage, not an unmasked per-clause mutation proof.
            import time
            from tests.waiting import wait_until
            stopped_at = home.hub.row_evidence(helpers.BOARD, "task_events", episode)["row"]["created_at"]
            wait_until(lambda: int(time.time()) > stopped_at, timeout=3)
            controls.put(home, "task_events", "promotion-" + uuid.uuid4().hex,
                         {"task_id": task, "kind": "promoted_manual", "payload": "{}", "created_at": int(time.time())})
            controls.put(home, "tasks", task, {"status": "ready"})
            home.resync()
            containment, held = _contain(home, task)
        else:
            event = home.hub.row_evidence(helpers.BOARD, "task_events", episode)["row"]
            assert json.loads(event["payload"])["cause"] == "safety_refusal"
    before = helpers.event_inventory(home, task)
    task_before = home.hub.row_evidence(helpers.BOARD, "tasks", task)["row"]
    replies = []
    actual_ops = helpers.ksc.Hub.ops

    def observe_ops(hub, board, ops, *args, **kwargs):
        result = actual_ops(hub, board, ops, *args, **kwargs)
        if any(op.get("op") == "authorize_owner_release" and op.get("task") == task for op in ops):
            replies.extend(result["results"])
        return result

    monkeypatch.setattr(helpers.ksc.Hub, "ops", observe_ops)
    first = helpers.release_cli(native, task, episode)
    value = json.loads(first.stdout)
    assert first.returncode == 2 and not value["ok"], (value, first.stderr)
    assert replies and replies[0]["status"] == "rejected", replies
    assert replies[0]["reason"] == {"safety": "owner_release_restricted", "containment": "containment_hold"}[hold], replies
    assert "release_read" not in value and value.get("publication_stage") != "published", value
    helpers.home_tick(home, monkeypatch, rounds=3)
    assert helpers.event_inventory(home, task) == before
    assert home.hub.row_evidence(helpers.BOARD, "tasks", task)["row"] == task_before
    assert not helpers.release_receipts(home, task)
    executor.resync()
    assert executor.hub.row_evidence(helpers.BOARD, "tasks", task)["row"] == task_before
    if hold == "containment":
        assert home.hub.containment_context(helpers.BOARD, task) == held
        assert home.hub.row_evidence(helpers.BOARD, "task_events", containment)["row"]["kind"] == "contained"
