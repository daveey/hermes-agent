"""Lease RPC acknowledgements fence feed echoes, not unrelated task fields."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def primary(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    for conn in support.synced_conn(home):
        sync.set_mode(conn, "primary")
        yield conn


@pytest.fixture
def hub(primary):
    class Hub:
        version = 29
        expires = int(time.time() + 900) * 1000
        holder = primary.execute("SELECT gateway_id FROM kanban_sync_state").fetchone()[0]

        def leases(self, board, body):
            return {"status": "applied", "version": self.version,
                    "lease": {"run_uid": body["run_uid"], "holder": self.holder,
                              "expires": self.expires}}

    hub = Hub()
    previous = sync.set_lease_client(client.HubLeaseClient(hub))
    try:
        yield hub
    finally:
        sync.set_lease_client(previous)


def row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def apply(conn, tid, version, holder, expires, op="upsert", **fields):
    sync.apply_remote_changes(conn, [sync.RemoteChange(
        version, "tasks", tid, op,
        {"lease_holder": holder, "lease_expires": expires, **fields},
        version, "", "gw_remote",
    )], new_cursor=version)


@pytest.mark.parametrize("op", ["upsert", "lease"])
def test_grant_fences_previous_release_and_intermediate_null(primary, hub, op):
    tid = kb.create_task(primary, title="original", assignee="worker")
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:1")
    before = row(primary, tid)
    # The grant's final version must NOT acknowledge earlier task fields.
    assert before["sync_version"] is None
    for version in (26, 28):
        apply(primary, tid, version, None, None, op=op,
              **({"title": "remote edit", "status": "running"} if op == "upsert" else {}))
        after = row(primary, tid)
        assert after["claim_expires"] == before["claim_expires"]
        assert after["lease_holder"] == hub.holder
        assert after["lease_expires"] == hub.expires
    apply(primary, tid, 29, hub.holder, hub.expires)
    after = row(primary, tid)
    assert after["claim_expires"] == before["claim_expires"]
    assert after["lease_version"] == 29
    if op == "upsert":
        assert after["title"] == "remote edit"
    assert "lease_version" not in sync.captured_columns(primary, "tasks")


@pytest.mark.parametrize("op", ["upsert", "lease"])
@pytest.mark.parametrize("holder", [None, "gw_other"])
def test_renewal_fences_old_echo_but_new_takeover_expires(primary, hub, monkeypatch, op, holder):
    tid = kb.create_task(primary, title="renew", assignee="worker")
    assert kb.claim_task(primary, tid, ttl_seconds=900, claimer="host:1")
    old_expires = hub.expires
    hub.version = 35
    hub.expires += 300000
    monkeypatch.setattr(sync, "_renewal_due", lambda *args: True)
    assert kb.heartbeat_claim(primary, tid, ttl_seconds=900, claimer="host:1")
    before = row(primary, tid)
    assert before["sync_version"] is None
    apply(primary, tid, 30, hub.holder, old_expires, op=op)
    assert row(primary, tid)["lease_expires"] == hub.expires
    assert row(primary, tid)["claim_expires"] == before["claim_expires"]
    assert row(primary, tid)["lease_version"] == 35
    apply(primary, tid, 36, holder, None if holder is None else hub.expires, op=op)
    after = row(primary, tid)
    assert after["lease_holder"] == holder
    assert after["lease_version"] == 36
    assert after["claim_expires"] < int(time.time())


def test_lease_only_feed_does_not_acknowledge_task_fields(primary, hub):
    tid = kb.create_task(primary, title="original", assignee="worker")
    assert kb.claim_task(primary, tid, ttl_seconds=900)
    apply(primary, tid, 35, hub.holder, hub.expires, op="lease")
    apply(primary, tid, 30, None, None, title="earlier field edit")
    assert row(primary, tid)["title"] == "earlier field edit"
    assert row(primary, tid)["lease_holder"] == hub.holder
    assert row(primary, tid)["lease_version"] == 35
