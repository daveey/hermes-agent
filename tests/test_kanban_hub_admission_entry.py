"""Every entry path that can put a card on the hub consults the admission judge. [daveey]

One test per path (CLI auto create, CLI runs_on machine:hub, CLI force, move to machine:hub,
capability owner routed to the hub, capability owner held locally by the hub); each checks the
deny and the admit outcome with a fake judge (never a live model).
"""
import argparse
import json
import time

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_capability_routing as routing_support
from tests.hermes_cli import test_kanban_worker_policy_db as fixtures
from tests.hermes_cli import hub_judge_support

conn = fixtures.conn
env = routing_support.env
hub_judge = hub_judge_support.hub_judge
MARKED = "Read ~/.hermes/logs/agent.log on the hub"


def _events(db, tid, kind="hub_admission"):
    return [e.payload for e in kb.list_events(db, tid) if e.kind == kind]


def _cli_create(*argv):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    return cli._cmd_create(parser.parse_args(["kanban", "create", *argv]))


@pytest.fixture
def hub_cli(conn, monkeypatch, hub_judge):
    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": []}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    return hub_judge


@pytest.mark.parametrize("admit", [True, False])
def test_cli_hub_auto_create(hub_cli, admit):
    hub_cli.admit_next = admit
    assert _cli_create(MARKED, "--hub-reason", "only the hub has it") == 0
    assert hub_cli.calls[0]["hub_reason"] == "only the hub has it"
    with kbc.connect_closing() as db:
        (task,) = kb.list_tasks(db)
        (event,) = _events(db, task.id)
    assert event["admit"] is admit
    if admit:
        assert task.placement_policy is None  # stays on the hub
    else:
        assert task.placement_policy == "worker-first-v1"
        assert "hub:write" in task.requires


@pytest.mark.parametrize("admit", [True, False])
def test_cli_runs_on_machine_hub(hub_cli, monkeypatch, admit):
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(wn, "check_machine_pins", lambda *a, **kw: None)
    hub_cli.admit_next = admit
    rc = _cli_create("Rotate the Board admin key", "--runs-on", "machine:hub")
    assert len(hub_cli.calls) == 1
    with kbc.connect_closing() as db:
        tasks = kb.list_tasks(db)
        if admit:
            assert rc == 0 and len(tasks) == 1
            row = db.execute("SELECT runs_on FROM tasks WHERE id=?", (tasks[0].id,)).fetchone()
            assert json.loads(row[0]) == ["machine:hub"]
            assert _events(db, tasks[0].id)[0]["admit"] is True
        else:
            assert rc == 2 and tasks == []  # refused: the creator re-places it


def test_cli_force_hub_outside_worker_is_recorded(hub_cli):
    assert _cli_create(MARKED, "--force-hub") == 0
    assert hub_cli.calls == []
    with kbc.connect_closing() as db:
        (task,) = kb.list_tasks(db)
        (event,) = _events(db, task.id)
    assert event["forced"] is True and event["admit"] is True
    assert "session" in event
    assert task.placement_policy is None


def test_cli_force_hub_inside_worker_is_refused(hub_cli, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert _cli_create(MARKED, "--force-hub") == 2
    with kbc.connect_closing() as db:
        assert kb.list_tasks(db) == []


@pytest.mark.parametrize("admit", [True, False])
def test_move_to_machine_hub(conn, monkeypatch, hub_judge, admit):
    from hermes_cli import kanban_move as km
    monkeypatch.setattr(placement, "config", dict)
    hub_judge.admit_next = admit
    tid = kb.create_task(conn, title="Analyze data", assignee="default")
    res = km.move_queued(conn, tid, runs_on=["machine:hub"], reason="needs the hub",
                         machines={"hub": {"state": "active", "pickup": False}}, me="hub")
    assert len(hub_judge.calls) == 1
    (event,) = _events(conn, tid)
    assert event["admit"] is admit and event["entry"] == "move"
    if admit:
        assert res.get("reason_code") != "hub_admission_denied"
    else:
        assert res["ok"] is False and res["reason_code"] == "hub_admission_denied"
        assert _events(conn, tid, "moved") == []


def test_move_to_a_worker_makes_no_judge_call(conn, hub_judge):
    from hermes_cli import kanban_move as km
    tid = kb.create_task(conn, title="Read logs on the hub", assignee="default")
    km.move_queued(conn, tid, runs_on=["machine:worker-1"], reason="portable",
                   machines={"worker-1": {"state": "active", "pickup": "all", "labels": []}}, me="hub")
    assert hub_judge.calls == []


def _route_to_hub(env, monkeypatch):
    mail = env["home"].parent / "mail.json"
    mail.write_text(json.dumps({"gateway": "macbook"}))
    routing_support.publish(env["hub"], "hub", {routing_support.NEED: time.time() - 60})


@pytest.mark.parametrize("admit", [True, False])
def test_capability_owner_routed_to_the_hub(env, monkeypatch, hub_judge, admit):
    _route_to_hub(env, monkeypatch)
    hub_judge.admit_next = admit
    with kbc.connect_closing() as db:
        tid = routing_support.running(db)
        if admit:
            out = kcr.route(db, tid, [routing_support.NEED], reason="Rotate the Board admin key")
            assert [r["gateway"] for r in out["routes"]] == ["hub"]
            assert env["calls"][0][2] == "hub"
        else:
            with pytest.raises(kcr.CapabilityRouteError) as exc:
                kcr.route(db, tid, [routing_support.NEED], reason="tail the hub logs")
            assert exc.value.code == "hub_admission_denied"
            assert env["calls"] == []  # nothing was sent to the hub
        (event,) = _events(db, tid)
    assert event["admit"] is admit and event["entry"] == "capability_owner"
    assert len(hub_judge.calls) == 1


@pytest.mark.parametrize("admit", [True, False])
def test_capability_owner_held_by_the_hub_itself(env, hub_judge, admit):
    hub_judge.admit_next = admit
    need = routing_support.NEED
    with kbc.connect_closing() as db:
        tid = routing_support.running(db)
        task = kb.get_task(db, tid)
        rid = kcr.request_id(tid, need)
        kcr._local_owner(db, task, {"request_id": rid, "generation": 1}, need=need, rid=rid,
                         self_gw="hub", from_gw="worker-1", reason="do the scoped action")
        owner = next(t for t in kb.list_tasks(db) if t.id != tid)
    if admit:
        assert owner.placement_policy is None
    else:
        assert owner.placement_policy == "worker-first-v1"
        assert owner.requires == ["hub:write"]
