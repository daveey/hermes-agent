"""Remote access is a declared contract, never inferred from arbitrary prose."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as node
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_coordination_placement as helpers
from tests.hermes_cli.test_kanban_worker_policy_db import conn

__all__ = ["conn"]

fleet = helpers.fleet


def card(body, requires=()):
    return {"title": "Check remote evidence", "body": body, "requires": list(requires)}


@pytest.mark.parametrize("body", [
    "on the hub, kill the stuck dispatcher pid",
    "delete the stale rows from kanban.db",
    "ssh hermes-hub and sudo reboot",
    "ssh worker-1",
    "read the log on the hub",
])
def test_undeclared_access_never_exempts_markers(fleet, body):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    assert kp.portable_reason(card(body), cfg) == "home gateway marker"


@pytest.mark.parametrize("requirement,label,body", [
    ("hub:read", "capability:hub-read", "read the log on the hub"),
    ("ssh:mbp", "capability:ssh-mbp", "ssh to the MacBook for a read"),
    ("ssh:mini", "capability:ssh-mini", "ssh mini diag"),
])
def test_declared_access_requires_every_destination(fleet, requirement, label, body):
    cfg, entry, machines = fleet
    args = card(body, [requirement])
    assert kp.portable_reason(args, cfg) is not None
    entry["labels"].append(label)
    assert kp.portable_reason(args, cfg) is None
    cfg["worker_pools"]["hub"].append("other")
    machines["other"] = dict(entry, labels=[])
    assert kp.portable_reason(args, cfg) is not None


def test_access_contract_is_rechecked_at_execution(fleet, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_placement as wp
    cfg, entry, _ = fleet
    args = card("read the log on the hub", ["hub:read"])
    entry["labels"] += helpers.ACCESS
    assert kp.portable_reason(args, cfg) is None
    monkeypatch.setattr(sync, "board_mode", lambda _: "primary")
    monkeypatch.setattr(sync, "sync_state", lambda _: {"gateway_id": "worker"})
    args.update(placement_policy="worker-first-v1", runs_on='["machine:worker"]')
    assert wp.execution_refusal(None, args) is None
    entry["labels"] = []
    assert wp.execution_refusal(None, args) is not None


@pytest.mark.parametrize("body", ["read ~/.hermes/state.db on the hub", "systemctl restart x on the hub"])
def test_independent_markers_survive_declared_access(fleet, body):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    assert kp.portable_reason(card(body, ["hub:read"]), cfg) is not None


def test_operator_extra_marker_survives_declared_access(fleet):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    cfg["extra_home_markers"] = [r"on the hub"]
    assert kp.portable_reason(card("read logs on the hub", ["hub:read"]), cfg) is not None


def test_legacy_routing_does_not_drop_access_contract(fleet):
    cfg, entry, _ = fleet
    entry["labels"] += helpers.ACCESS
    cfg["heavy_route"] = {"hub": "worker"}
    args = card("read logs on the hub", ["hub:read"])
    args["title"] = "Run pytest"
    assert kp._routeless_heavy(args, kp._Cfg(cfg), "hub", lambda _: True) is None


@pytest.mark.parametrize("policy,expected", [
    ("worker-first-v1", ["aws:example:read"]),
    (None, ["hub:read", "aws:example:read"]),
])
def test_preflight_keeps_enrolled_access_with_worker(monkeypatch, policy, expected):
    from types import SimpleNamespace
    from hermes_cli import kanban_capability_routing as routing
    from hermes_cli import kanban_db as kb
    task = SimpleNamespace(requires=["hub:read", "aws:example:read"],
                           placement_policy=policy, status="ready", body="read", title="read")
    monkeypatch.setattr(kb, "get_task", lambda *_: task)
    monkeypatch.setattr(routing, "_previous_routes", lambda *_: False)
    observed = []
    def route(conn, task_id, needs, **kwargs):
        observed.extend(needs)
        return {"test": True}
    monkeypatch.setattr(routing, "route", route)
    assert routing.preflight(None, "test") == {"test": True}
    assert observed == expected


@pytest.mark.parametrize("runs_on,requires,expected", [
    (["pool:worker", "capability:hub-read"], ["terminal", "hub:read"], []),
    ('["pool:worker", "capability:hub-write"]', ["terminal", "hub:write"], []),
    (["capability:ssh-mini"], ["ssh:mini", "aws:example:read"], ["aws:example:read"]),
    (["pool:worker"], ["terminal", "hub:read"], ["hub:read"]),
    (["capability:hub-read"], ["hub:write"], ["hub:write"]),
    (None, ["hub:read"], ["hub:read"]),
])
def test_preflight_drops_access_needs_the_runs_on_label_carries(monkeypatch, runs_on, requires, expected):
    from types import SimpleNamespace
    from hermes_cli import kanban_capability_routing as routing
    from hermes_cli import kanban_db as kb
    task = SimpleNamespace(id="t_x", requires=requires, runs_on=runs_on, placement_policy=None,
                           status="ready", body="read", title="read")
    monkeypatch.setattr(kb, "get_task", lambda *_: task)
    monkeypatch.setattr(routing, "_previous_routes", lambda *_: False)
    observed = []

    def route(conn, task_id, needs, **kwargs):
        observed.extend(needs)
        return {"warning": "no holder", "code": "no_capability_holder"}
    monkeypatch.setattr(routing, "route", route)
    placed = routing.preflight(None, "t_x")
    assert [n for n in observed if n != "terminal"] == expected
    assert (placed is None) == (not expected)


@pytest.mark.parametrize("requirement,label", [
    ("hub:read", "capability:hub-read"),
    ("hub:write", "capability:hub-write"),
    ("ssh:mini", "capability:ssh-mini"),
    ("ssh:mbp", "capability:ssh-mbp"),
])
@pytest.mark.parametrize("carried", [False, True])
def test_preflight_reads_persisted_access_labels(conn, monkeypatch, requirement, label, carried):
    from hermes_cli import kanban_capability_routing as routing

    tid = kb.create_task(conn, title="Check declared access", assignee="default",
                         requires=["terminal", requirement])
    # Exercise the production Task and SQL read, not a mock carrying runs_on.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET placement_policy=NULL, runs_on=? WHERE id=?",
                     (json.dumps(["pool:worker", label] if carried else ["pool:worker"]), tid))
    task = kb.get_task(conn, tid)
    assert task is not None
    assert not hasattr(task, "runs_on")
    assert task.placement_policy is None
    monkeypatch.setattr(routing, "has_replica", lambda: True)
    result = routing.preflight(conn, tid, self_gw="hub", local={}, remote={})
    if carried:
        assert result is None
    else:
        assert result is not None
        assert result["code"] == "no_capability_holder", result
    unchanged = kb.get_task(conn, tid)
    assert unchanged is not None and unchanged.status == "ready"


@pytest.mark.parametrize("requirement", ["hub:read", "hub:write", "ssh:mini", "ssh:mbp"])
def test_preflight_keeps_access_needs_without_sync_schema(tmp_path, monkeypatch, requirement):
    from hermes_cli import kanban_capability_routing as routing
    from tests.hermes_cli import kanban_sync_support as support

    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, False)
    with kbc.connect(home / "kanban.db") as db:
        assert "runs_on" not in {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        tid = kb.create_task(db, title="Check declared access", assignee="default",
                             requires=["terminal", requirement], placement_policy=None)
        assert kb.get_task(db, tid).placement_policy is None
        monkeypatch.setattr(routing, "has_replica", lambda: True)
        result = routing.preflight(db, tid, self_gw="hub", local={}, remote={})
        assert result is not None
        assert result["code"] == "no_capability_holder", result
        assert requirement in result["warning"], result
        assert kb.get_task(db, tid).status == "ready"


@pytest.mark.parametrize("requirement,label,body", [
    ("hub:read", "capability:hub-read", "read the log on the hub"),
    ("ssh:mbp", "capability:ssh-mbp", "ssh to the MacBook for a read"),
    ("ssh:mini", "capability:ssh-mini", "ssh mini diag"),
])
def test_db_access_contract_survives_replica_import_and_revocation(
        conn, fleet, tmp_path, monkeypatch, requirement, label, body):
    cfg, entry, _ = fleet
    entry["labels"].append(label)
    monkeypatch.setattr(kp, "config", lambda: cfg)
    needs = ["terminal", requirement]
    tid = kb.create_task(conn, title="Check remote evidence", body=body,
                         assignee="default", requires=needs)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.requires == needs
    assert task.placement_policy == wp.POLICY
    captured = conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' "
        "AND row_uid=? ORDER BY rowid LIMIT 1", (tid,)).fetchone()
    fields = json.loads(captured["fields"])
    assert json.loads(fields["requires"]) == needs
    assert fields["placement_policy"] == wp.POLICY
    # The Board assigns the destination; creation itself must not manufacture one.
    assert fields["runs_on"] is None
    fields.update(runs_on=json.dumps(["machine:worker"]), created_on="hub")
    change = sync.RemoteChange(1, "tasks", tid, "upsert", fields, 1, "", "hub")
    replica_path = tmp_path / "replica.db"
    with kbc.connect(replica_path) as replica:
        client.ensure_identity(replica, "default", "worker")
        sync.apply_remote_changes(replica, [change], new_cursor=1, observers=False)
        replicated = kb.get_task(replica, tid)
        assert replicated is not None and replicated.requires == needs
    # Reopen the replica so pickup consumes persisted bytes, not the creator's objects.
    with kbc.connect(replica_path) as replica, kbc.connect(tmp_path / "worker.db") as worker:
        client.ensure_identity(worker, "default", "worker")
        sync.set_mode(worker, "primary")
        assert node.import_cards(worker, replica, [tid]) == [tid]
        imported = kb.get_task(worker, tid)
        assert imported is not None
        assert imported.requires == needs
        assert imported.placement_policy == wp.POLICY
        row = worker.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert json.loads(row["runs_on"]) == ["machine:worker"]
        assert wp.execution_refusal(worker, row) is None
        assert worker.execute("SELECT count(*) FROM kanban_sync_outbox").fetchone()[0] == 0
        entry["labels"].remove(label)
        assert wp.execution_refusal(worker, row) == "needs worker coordination capability"
        assert wp.local_refusals(worker, "ready")[tid] == "needs worker coordination capability"
        assert kb.claim_task(worker, tid) is None
        refused = kb.get_task(worker, tid)
        assert refused is not None and refused.status == "ready"
        assert worker.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


@pytest.mark.parametrize("requirement,label,body", [
    ("hub:read", "capability:hub-read", "read the log on the hub"),
    ("ssh:mbp", "capability:ssh-mbp", "ssh to the MacBook for a read"),
    ("ssh:mini", "capability:ssh-mini", "ssh mini diag"),
])
@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "second-destination-unattested"])
def test_db_creation_does_not_enroll_without_full_access_contract(
        conn, fleet, monkeypatch, requirement, label, body, declared):
    cfg, entry, machines = fleet
    entry["labels"].append(label)
    if declared:
        cfg["worker_pools"]["hub"].append("other")
        machines["other"] = dict(entry, labels=[])
    monkeypatch.setattr(kp, "config", lambda: cfg)
    needs = [requirement] if declared else []
    tid = kb.create_task(conn, title="Check remote evidence", body=body,
                         assignee="default", requires=needs)
    task = kb.get_task(conn, tid)
    assert task is not None
    assert task.placement_policy is None
    assert (task.requires or []) == needs
    captured = conn.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='tasks' "
        "AND row_uid=? ORDER BY rowid LIMIT 1", (tid,)).fetchone()
    assert json.loads(captured["fields"])["placement_policy"] is None
