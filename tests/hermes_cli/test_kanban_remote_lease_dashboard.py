"""Dashboard lifecycle writes on a card another machine runs under its live Board lease are refused up front. [daveey]

Before: on the creator (no local claim; the lease is only mirrored as ``lease_holder``/``lease_expires``) a drag to
done/blocked/ready, a bulk status change or a reclaim committed locally and answered 200; then the Board rejected the
status write (``live_claim``) while the unfenced task_runs/task_events rows leaked to it, exactly as the CLI did in
t_01a1ccfeab615947. Now each answers 409 with ``kanban_remote_stop.refusal`` naming the holder and writes nothing,
like the CLI verbs and kanban_* tools. Archive keeps its stop-request route (test_kanban_remote_archive).
"""

from __future__ import annotations


import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_remote_stop import refusal
from tests.hermes_cli.test_kanban_remote_archive import _running_on_worker
from tests.hermes_cli import test_kanban_remote_lease_status_writes as _sw
from tests.hermes_cli.test_kanban_worker_node import WORKER

# The two-gateway fixtures: hub creates the card, worker-1 runs it under its Board lease.
svc, gws, kills, _snapshot, _run_row = _sw.svc, _sw.gws, _sw.kills, _sw._snapshot, _sw._run_row
API = "/api/plugins/kanban"


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tests.plugins.test_kanban_dashboard_plugin import _load_plugin_router
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix=API)
    return TestClient(app)


def _remote(gws, svc):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    return hub, tid


def _assert_refused_409(r, conn, tid, before, run_before, verb):
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message"] == refusal(verb, tid, WORKER), detail
    assert detail["holder"] == WORKER and detail["rejection"] == "live_claim", detail
    assert _snapshot(conn) == before  # tasks, task_runs, task_events, kanban_sync_outbox: untouched
    assert kb.get_task(conn, tid).status == "running"
    assert _run_row(conn, tid) == run_before


_PATCHES = [
    ("complete", {"status": "done", "summary": "done elsewhere"}),
    ("block", {"status": "blocked", "block_reason": "stop", "wake_check": "true", "check_every": 300}),
    ("schedule", {"status": "scheduled", "block_reason": "later", "clock_only": True}),
    ("request review", {"status": "review", "summary": "x"}),
    ("reclaim", {"status": "ready"}),
    ("reclaim", {"status": "todo"}),
    ("reclaim", {"status": "triage"}),
]


@pytest.mark.parametrize("verb,body", _PATCHES, ids=[b["status"] for _, b in _PATCHES])
def test_patch_status_under_a_remote_lease_is_refused_and_writes_nothing(gws, svc, kills, verb, body):
    hub, tid = _remote(gws, svc)
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    r = _client().patch(f"{API}/tasks/{tid}", json=body)
    _assert_refused_409(r, hub.conn, tid, before, run_before, verb)
    assert kills == []


def test_patch_status_with_an_assignee_writes_neither(gws, svc, kills):
    hub, tid = _remote(gws, svc)
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    r = _client().patch(f"{API}/tasks/{tid}", json={"status": "done", "assignee": "other", "priority": 5})
    _assert_refused_409(r, hub.conn, tid, before, run_before, "complete")
    assert kb.get_task(hub.conn, tid).assignee == "default"


@pytest.mark.parametrize("body", [{"status": "done"}, {"status": "blocked", "block_reason": "stop",
                                                       "wake_check": "true", "check_every": 300},
                                  {"status": "ready"}, {"assignee": "other", "reclaim_first": True}],
                         ids=["done", "blocked", "ready", "reassign-reclaim"])
def test_bulk_status_under_a_remote_lease_is_refused_per_id_and_writes_nothing(gws, svc, kills, body):
    hub, tid = _remote(gws, svc)
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    r = _client().post(f"{API}/tasks/bulk", json={"ids": [tid], "priority": 7, **body})
    assert r.status_code == 200, r.text
    verb = {"done": "complete", "blocked": "block", "ready": "reclaim"}.get(body.get("status"), "reassign")
    assert r.json()["results"] == [{"id": tid, "ok": False, "holder": WORKER, "error": refusal(verb, tid, WORKER)}]
    assert _snapshot(hub.conn) == before
    assert _run_row(hub.conn, tid) == run_before


def test_bulk_refusal_of_one_card_does_not_stop_its_siblings(gws, svc, kills):
    hub, tid = _remote(gws, svc)
    other = kb.create_task(hub.conn, title="queued here", assignee="default", runs_on=["machine:hub"])
    r = _client().post(f"{API}/tasks/bulk", json={"ids": [other, tid], "status": "done", "summary": "x"})
    assert r.status_code == 200, r.text
    res = {e["id"]: e for e in r.json()["results"]}
    assert res[other] == {"id": other, "ok": True}
    assert res[tid]["ok"] is False and res[tid]["holder"] == WORKER
    assert kb.get_task(hub.conn, other).status == "done"
    assert kb.get_task(hub.conn, tid).status == "running"


@pytest.mark.parametrize("endpoint", ["reclaim", "terminate", "reassign"])
def test_reclaim_endpoints_under_a_remote_lease_are_refused(gws, svc, kills, endpoint):
    hub, tid = _remote(gws, svc)
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    client = _client()
    if endpoint == "reclaim":
        r = client.post(f"{API}/tasks/{tid}/reclaim", json={"reason": "stuck"})
    elif endpoint == "terminate":
        run_id = hub.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        r = client.post(f"{API}/runs/{run_id}/terminate", json={"reason": "stuck"})
    else:
        r = client.post(f"{API}/tasks/{tid}/reassign", json={"profile": "other", "reclaim_first": True})
    _assert_refused_409(r, hub.conn, tid, before, run_before, "reassign" if endpoint == "reassign" else "reclaim")
    assert kills == []


def test_patch_title_under_a_remote_lease_still_works(gws, svc, kills):
    """Only lifecycle writes are fenced; a title edit is not one."""
    hub, tid = _remote(gws, svc)
    r = _client().patch(f"{API}/tasks/{tid}", json={"title": "renamed"})
    assert r.status_code == 200, r.text
    assert kb.get_task(hub.conn, tid).title == "renamed"


def test_unknown_status_under_a_remote_lease_is_still_a_400(gws, svc, kills):
    _hub, tid = _remote(gws, svc)
    assert _client().patch(f"{API}/tasks/{tid}", json={"status": "running"}).status_code == 400


# --- regressions: no live remote lease, no refusal -------------------------------------------------------------

def _cleared(hub, tid):
    # Model canonical lease release, not merely passage of its timestamp.
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_holder = NULL, lease_expires = NULL WHERE id = ?", (tid,))


def _own(hub, tid):
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET lease_holder = ? WHERE id = ?", (str(sync._state(hub.conn)["gateway_id"]), tid))


@pytest.mark.parametrize("lease", [_cleared, _own], ids=["board-cleared", "own-gateway"])
def test_patch_done_without_a_live_remote_lease_completes_as_before(gws, svc, kills, lease):
    hub, tid = _remote(gws, svc)
    lease(hub, tid)
    r = _client().patch(f"{API}/tasks/{tid}", json={"status": "done", "summary": "x"})
    assert r.status_code == 200, r.text
    assert kb.get_task(hub.conn, tid).status == "done"


@pytest.mark.parametrize("lease", [_cleared, _own], ids=["board-cleared", "own-gateway"])
def test_bulk_ready_without_a_live_remote_lease_moves_as_before(gws, svc, kills, lease):
    hub, tid = _remote(gws, svc)
    lease(hub, tid)
    r = _client().post(f"{API}/tasks/bulk", json={"ids": [tid], "status": "ready"})
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [{"id": tid, "ok": True}]
    assert kb.get_task(hub.conn, tid).status == "ready"
