"""Durable queue fences survive disabled sync and origin restart."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    with kbc.connect(home / "kanban.db") as db:
        ksc.ensure_identity(db, "default", "hub")
        sync.set_mode(db, "primary")
        yield db


def test_policy_is_captured_in_first_insert_with_parent_and_origin(conn):
    parent = kb.create_task(conn, title="Parent", assignee="default")
    tid = kb.create_task(conn, title="Public analysis", assignee="default", parents=[parent],
                         session_id="origin-session", placement_policy="worker-first-v1")
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["placement_policy"] == "worker-first-v1"
    assert row["session_id"] == "origin-session"
    assert row["status"] == "todo"
    assert conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (tid,)).fetchone()[0] == parent
    inserts = conn.execute("SELECT fields FROM kanban_sync_outbox WHERE row_uid=? ORDER BY rowid", (tid,)).fetchall()
    assert json.loads(inserts[0][0])["placement_policy"] == "worker-first-v1"
    assert kb.get_task(conn, tid).placement_policy == "worker-first-v1"


@pytest.mark.parametrize("creator", ["self-heal", "user", "default"])
def test_direct_producer_enrolls_portable_card(conn, monkeypatch, creator):
    from hermes_cli import kanban_placement as placement

    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    parent = kb.create_task(conn, title="Parent", assignee="default", runs_on=[])
    tid = kb.create_task(conn, title="Analyze public data", assignee="default", created_by=creator,
                         creator_task_id=parent, session_id="origin-chat", requires=["terminal"])
    assert kb.get_task(conn, tid).placement_policy == "worker-first-v1"
    inserts = conn.execute("SELECT fields FROM kanban_sync_outbox WHERE row_uid=? ORDER BY rowid", (tid,)).fetchall()
    assert json.loads(inserts[0][0])["placement_policy"] == "worker-first-v1"


@pytest.mark.parametrize("fields", [
    {"runs_on": []}, {"runs_on": ["linux"]}, {"workspace_kind": "dir", "workspace_path": "/repo"},
    {"board": "other"}, {"requires": ["host_repo"]}, {"placement_checked": True},
])
def test_direct_producer_keeps_placement_exclusions(conn, monkeypatch, fields):
    from hermes_cli import kanban_placement as placement

    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    tid = kb.create_task(conn, title="Analyze public data", assignee="default", **fields)
    assert kb.get_task(conn, tid).placement_policy is None


@pytest.mark.parametrize("title,reason", [("Analyze public data", None), ("Inspect this host", "home gateway marker")])
def test_direct_producer_records_authoritative_audit(conn, monkeypatch, title, reason):
    from hermes_cli import kanban_placement as placement
    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    tid = kb.create_task(conn, title=title, assignee="default", created_by="owner-wake")
    created = next(e for e in kb.list_events(conn, tid) if e.kind == "created")
    assert "portable_reason" in created.payload
    assert created.payload["portable_reason"] == reason


def test_direct_producer_uses_connection_board(conn, monkeypatch):
    from hermes_cli import kanban_placement as placement

    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    kb.create_board("other")
    with kbc.connect(board="other") as other:
        tid = kb.create_task(other, title="Analyze public data", assignee="default")
        assert kb.get_task(other, tid).placement_policy is None


def test_courier_keeps_immutable_placement(conn, monkeypatch):
    from hermes_cli import kanban_placement as placement
    from tests.hermes_cli.test_kanban_origin_creation import intent

    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    tid = kb.create_task(conn, title="Analyze public data", assignee="default", origin_intent=intent())
    assert kb.get_task(conn, tid).placement_policy is None
    assert kb.create_task(conn, title="Analyze public data", assignee="default", origin_intent=intent()) == tid
    envelope = json.loads(conn.execute("SELECT intent_json FROM origin_creation_intents").fetchone()[0])
    assert "placement_checked" not in envelope["options"]


@pytest.mark.parametrize("mode", ["off", "shadow", "primary"])
@pytest.mark.parametrize("lane", ["ready", "review"])
def test_queue_fence_precedes_direct_claim_and_lease_even_without_sync(conn, mode, lane):
    tid = kb.create_task(conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    conn.execute("UPDATE tasks SET status=? WHERE id=?", (lane, tid))
    conn.commit()
    sync.set_mode(conn, mode)
    client = support.FakeLeaseClient()  # would grant if accidentally consulted
    old = sync.set_lease_client(client)
    try:
        claim = kb.claim_task if lane == "ready" else kb.claim_review_task
        assert claim(conn, tid) is None
        assert client.calls == []
        assert sync.pop_claim_deferral(tid) == "waiting_for_worker_capacity"
        assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
    finally:
        sync.set_lease_client(old)


@pytest.mark.parametrize("mode", ["off", "shadow", "primary"])
def test_local_dispatch_refusal_survives_registry_failure_and_disabled_policy(conn, mode, monkeypatch):
    tid = kb.create_task(conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    sync.set_mode(conn, mode)
    monkeypatch.setattr(wn, "registry", lambda: None)
    assert wn.local_refusals(conn, "ready")[tid] == "waiting_for_worker_capacity"



@pytest.mark.parametrize("sample,capacity,allowed", [
    ({"cpu_load1": 3, "cpus": 4, "mem_used_pct": 50, "active_card_runs": 1}, 2, True),
    ({"cpu_load1": 3.4, "cpus": 4, "mem_used_pct": 50, "active_card_runs": 1}, 2, False),
    ({"cpu_load1": 0, "cpus": 4, "mem_used_pct": 85, "active_card_runs": 1}, 2, False),
    ({"cpu_load1": 0, "cpus": 4, "mem_used_pct": 50, "active_card_runs": 2}, 2, False),
    ({"cpu_load1": 0, "cpus": 4, "mem_used_pct": 50, "active_card_runs": None}, 2, False),
    ({"cpu_load1": float("nan"), "cpus": 4, "mem_used_pct": 50, "active_card_runs": 0}, 2, False),
    ({"cpu_load1": 0, "cpus": 0, "mem_used_pct": 50, "active_card_runs": 0}, 2, False),
    ({"cpu_load1": 0, "cpus": 4, "mem_used_pct": 50, "active_card_runs": 0}, None, False),
    ({"cpu_load1": 0, "cpus": 4, "mem_used_pct": 50, "active_card_runs": 0}, True, False),
])
@pytest.mark.parametrize("status", ["ready", "review"])
def test_worker_local_admission_rechecks_load_and_releases_unused_lease(conn, monkeypatch, sample, capacity, allowed, status):
    from hermes_cli import machine_metrics
    tid = kb.create_task(conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    # A worker ("hub" in this fixture) claiming another origin's card keeps the load formula (review ruling 2).
    conn.execute("UPDATE tasks SET runs_on=?, status=?, created_on=? WHERE id=?", ('["machine:hub"]', status, "origin-x", tid))
    conn.commit()
    monkeypatch.setattr(machine_metrics, "collect", lambda: sample)
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"hub": {"max_card_runs": capacity}}})
    client = support.FakeLeaseClient()
    old = sync.set_lease_client(client)
    try:
        claimed = (kb.claim_task if status == "ready" else kb.claim_review_task)(conn, tid)
        assert (claimed is not None) == allowed
        assert client.count("acquire") == 1
        assert client.count("release") == (0 if allowed else 1)
        if not allowed:
            assert sync.pop_claim_deferral(tid) == "waiting_for_worker_capacity"
    finally:
        sync.set_lease_client(old)


@pytest.mark.parametrize("fields", [{"runs_on": []}, {"runs_on": ["linux"]}, {"workspace_kind": "dir", "workspace_path": "/work"}])
def test_policy_cannot_be_combined_with_manual_or_host_placement(conn, fields):
    with pytest.raises(ValueError, match="worker-first"):
        kb.create_task(conn, title="Public analysis", placement_policy="worker-first-v1", **fields)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("broken", ["registry", "metrics"])
def test_admission_sampling_failure_releases_grant(conn, monkeypatch, broken):
    from hermes_cli import machine_metrics
    tid = kb.create_task(conn, title="Public analysis", placement_policy="worker-first-v1")
    conn.execute("UPDATE tasks SET runs_on=?, created_on=? WHERE id=?", ('["machine:hub"]', "origin-x", tid))
    conn.commit()
    def unavailable():
        raise OSError("sample unavailable")
    monkeypatch.setattr(wn, "registry", unavailable if broken == "registry" else lambda: {"machines": {}})
    monkeypatch.setattr(machine_metrics, "collect", unavailable)
    client = support.FakeLeaseClient()
    old = sync.set_lease_client(client)
    try:
        assert kb.claim_task(conn, tid) is None
        assert client.count("release") == 1
    finally:
        sync.set_lease_client(old)


def test_unknown_policy_is_rejected_before_insertion(conn):
    with pytest.raises(ValueError, match="placement policy"):
        kb.create_task(conn, title="Public analysis", placement_policy="worker-first-v999")
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["ready", "review"])
def test_origin_claiming_its_own_card_skips_worker_load_formula(conn, monkeypatch, status):
    """Review ruling 2: hub overflow is gated by measured CPU (claim_transaction), never by load1/CPUs."""
    from hermes_cli import machine_metrics
    tid = kb.create_task(conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    conn.execute("UPDATE tasks SET runs_on=?, status=?, created_on=? WHERE id=?", ('["machine:hub"]', status, "hub", tid))
    conn.commit()
    monkeypatch.setattr(machine_metrics, "collect", lambda: {"cpu_load1": 400, "cpus": 4, "mem_used_pct": 50,
                                                             "active_card_runs": 0})
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"hub": {"max_card_runs": 7}}})
    client = support.FakeLeaseClient()
    old = sync.set_lease_client(client)
    try:
        assert (kb.claim_task if status == "ready" else kb.claim_review_task)(conn, tid) is not None
        assert client.count("release") == 0
    finally:
        sync.set_lease_client(old)
