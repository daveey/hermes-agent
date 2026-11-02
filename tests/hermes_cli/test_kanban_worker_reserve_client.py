"""Frozen Board reservation contract and origin-only ready reevaluation."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_policy_db as db_fixtures
from tests.hermes_cli import test_kanban_worker_node as node_fixtures

conn = db_fixtures.conn
gws = node_fixtures.gws
svc = node_fixtures.svc


def test_sync_tick_reserves_only_after_task_push_ack(gws, monkeypatch):
    origin, _ = gws
    origin.enter()
    tid = kb.create_task(origin.conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    calls = []
    def observe(db, hub, board, gateway):
        row = db.execute("SELECT sync_version, placement_policy FROM tasks WHERE id=?", (tid,)).fetchone()
        calls.append((board, gateway, row[0], row[1]))
        return 0
    monkeypatch.setattr(wp, "reevaluate", observe)
    origin.round()
    assert len(calls) == 1
    assert calls[0][:2] == ("default", "hub")
    assert isinstance(calls[0][2], int) and calls[0][2] > 0
    assert calls[0][3] == "worker-first-v1"


def test_dispatch_tick_reevaluates_before_local_spawn_budget(gws, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    origin, _ = gws
    origin.enter()
    tid = kb.create_task(origin.conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    calls = []
    monkeypatch.setattr(wp, "dispatch_tick", lambda db: calls.append(tid), raising=False)
    monkeypatch.setattr(dispatch, "_tick_spawn_budget", lambda *a, **kw: (False, 0))
    result = origin.tick(reconcile_orphans=False)
    assert calls == [tid]
    assert not result.spawned
    assert kb.get_task(origin.conn, tid).status == "ready"


def test_queued_origin_cannot_spawn_even_with_local_capacity(gws, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    origin, _ = gws
    origin.enter()
    tid = kb.create_task(origin.conn, title="Public analysis", assignee="default", placement_policy=wp.POLICY)
    monkeypatch.setattr(wp, "dispatch_tick", lambda db: None)
    monkeypatch.setattr(dispatch, "_tick_spawn_budget", lambda *a, **kw: (False, 8))
    assert not dispatch.has_spawnable_ready(origin.conn)
    result = origin.tick(reconcile_orphans=False)
    assert not result.spawned
    assert kb.get_task(origin.conn, tid).status == "ready"
    assert origin.conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0


def test_acknowledged_policy_sync_does_not_replay_stale_assignment_fields(gws):
    origin, _ = gws
    origin.enter()
    tid = kb.create_task(origin.conn, title="Public analysis", placement_policy=wp.POLICY)
    origin.round()
    origin.conn.execute("UPDATE tasks SET priority=5 WHERE id=?", (tid,))
    origin.conn.commit()
    # A queued pre-assignment snapshot must not erase the Board-owned labels or
    # re-enroll a policy that an explicit rollback repin has since removed.
    origin.conn.execute("UPDATE kanban_sync_outbox SET fields=? WHERE table_name='tasks' AND row_uid=? AND state='pending'",
                        (json.dumps({"priority": 5, "placement_policy": wp.POLICY, "runs_on": None}), tid))
    origin.conn.commit()
    hub = origin.svc.hub(origin.name)
    sent, original = [], hub.ops
    def observe(board, ops):
        sent.extend(ops)
        return original(board, ops)
    hub.ops = observe
    result = ksc.push(origin.conn, hub, "default", origin.name, primary=True)
    assert result.rejected == 0
    fields = next(op["fields"] for op in sent if op.get("uid") == tid)
    assert fields["priority"] == 5
    assert "runs_on" not in fields and "placement_policy" not in fields


@pytest.mark.parametrize("action", ["reserve", "repin"])
def test_reserve_http_contract(action):
    received = []
    # Exact queued response contract from worker/src/worker_placement.ts reserveWorker.
    response = {"state": "queued", "machine": None, "reservation_id": None,
                "expires_at": None, "reason": "waiting_for_worker_capacity"}
    fields = {"candidates": ["worker-1"]}
    wire = {"policy": "worker-first-v1", **fields}
    if action == "repin":
        response = {"version": 8, "runs_on": ["machine:hub"], "placement_policy": None}
        fields = wire = {"runs_on": ["machine:hub"]}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        hub = ksc.Hub(ksc.Identity(f"http://127.0.0.1:{server.server_port}", "synthetic", "hub"))
        assert getattr(hub, action)("default", task_id="task", expected_revision=7, **fields) == response
        assert received == [(f"/v2/boards/default/placement/{action}", {
            "task_id": "task", "expected_revision": 7, **wire})]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RecordingHub:
    def __init__(self):
        self.calls = []

    def reserve(self, board, **body):
        self.calls.append((board, body))
        return {"state": "queued", "machine": None, "reservation_id": None,
                "expires_at": None, "reason": "waiting_for_worker_capacity"}


def acknowledged(conn, **fields):
    tid = kb.create_task(conn, title="Public analysis", assignee="default", placement_policy="worker-first-v1")
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET sync_version=4, created_on='hub' WHERE id=?", (tid,))
    for key, value in fields.items():
        conn.execute(f"UPDATE tasks SET {key}=? WHERE id=?", (value, tid))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.execute("DELETE FROM kanban_sync_outbox")
    conn.commit()
    return tid


def test_reevaluation_sends_revision_but_never_assigns_from_response(conn, monkeypatch):
    tid = acknowledged(conn)
    hub = RecordingHub()
    pulls = []
    monkeypatch.setattr(ksc, "pull", lambda *args, **kwargs: pulls.append(kwargs))
    assert wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}}) == 1
    assert hub.calls == [("default", {"task_id": tid, "expected_revision": 4, "candidates": ["worker-1"]})]
    assert pulls == [{"observers": True, "partition": "hub"}]
    assert conn.execute("SELECT runs_on FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None


@pytest.mark.parametrize("fields", [
    {"status": "running"}, {"status": "review"}, {"status": "todo"}, {"status": "blocked"},
    {"lease_holder": "worker-1", "lease_expires": 1}, {"claim_lock": "local:123"},
    {"created_on": "other"}, {"sync_version": None}, {"placement_policy": None},
])
def test_reevaluation_never_moves_nonready_leased_foreign_or_manual_cards(conn, fields):
    acknowledged(conn, **fields)
    hub = RecordingHub()
    assert wp.reevaluate(conn, hub, "default", "hub", cfg={"worker_pools": {"hub": ["worker-1"]}}) == 0
    assert hub.calls == []


@pytest.mark.parametrize("cfg", [{}, {"worker_pools": {"hub": []}}, {"worker_pools": {"hub": "worker-1"}}])
def test_disabled_or_invalid_pool_retains_existing_fence(conn, cfg):
    tid = acknowledged(conn)
    hub = RecordingHub()
    assert wp.reevaluate(conn, hub, "default", "hub", cfg=cfg) == 0
    assert kb.get_task(conn, tid).placement_policy == "worker-first-v1"
    assert hub.calls == []
