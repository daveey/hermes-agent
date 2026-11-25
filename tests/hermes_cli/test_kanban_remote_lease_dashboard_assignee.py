"""Dashboard assignee writes on a card another machine runs under its live Board lease are refused up front. [daveey]

Before: PATCH /tasks/{id} ``{"assignee": ...}`` and POST /tasks/bulk ``{"assignee": ...}`` (without
``reclaim_first``) on the creator of a card a worker node runs under its Board lease called ``assign_task`` locally
and answered 200. The Board accepts an assignee write from any gateway, so a non-holder changed who owns a live run.
Now both answer like ``hermes kanban reassign``: 409 (bulk: a per-id ``ok: false``) with
``kanban_remote_stop.refusal("reassign", ...)`` naming the holder, and the database left byte-for-byte as it was.
The status/reclaim siblings are in test_kanban_remote_lease_dashboard.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_remote_stop import refusal
from tests.hermes_cli import test_kanban_remote_lease_dashboard as _dash
from tests.hermes_cli.test_kanban_worker_node import WORKER

svc, gws, kills, _snapshot, _run_row = _dash.svc, _dash.gws, _dash.kills, _dash._snapshot, _dash._run_row
API, _client, _remote, _cleared, _own = _dash.API, _dash._client, _dash._remote, _dash._cleared, _dash._own


def _board_assignee(hub, tid):
    hub.round()  # whatever the hub wrote is pushed, and the Board's row pulled back
    return kb.get_task(hub.conn, tid).assignee


@pytest.mark.parametrize("body", [{"assignee": "other"}, {"assignee": ""}, {"assignee": "other", "title": "renamed"},
                                  {"assignee": "other", "status": "archived"}],
                         ids=["assign", "unassign", "with-title", "with-archive"])
def test_patch_assignee_under_a_remote_lease_is_refused_and_writes_nothing(gws, svc, kills, body):
    hub, tid = _remote(gws, svc)
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    r = _client().patch(f"{API}/tasks/{tid}", json=body)
    _dash._assert_refused_409(r, hub.conn, tid, before, run_before, "reassign")
    task = kb.get_task(hub.conn, tid)
    assert (task.assignee, task.title) == ("default", "remote work")
    assert _board_assignee(hub, tid) == "default"  # the live run's owner did not change anywhere
    assert kills == []


@pytest.mark.parametrize("body", [{"assignee": "other"}, {"assignee": ""}], ids=["assign", "unassign"])
def test_bulk_assignee_under_a_remote_lease_is_refused_per_id_and_writes_nothing(gws, svc, kills, body):
    hub, tid = _remote(gws, svc)
    before, run_before = _snapshot(hub.conn), _run_row(hub.conn, tid)
    r = _client().post(f"{API}/tasks/bulk", json={"ids": [tid], "priority": 7, **body})
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [{"id": tid, "ok": False, "holder": WORKER,
                                    "error": refusal("reassign", tid, WORKER)}]
    assert _snapshot(hub.conn) == before
    assert _run_row(hub.conn, tid) == run_before
    assert _board_assignee(hub, tid) == "default"


def test_bulk_assignee_refusal_of_one_card_does_not_stop_its_siblings(gws, svc, kills):
    hub, tid = _remote(gws, svc)
    other = kb.create_task(hub.conn, title="queued here", assignee="default", runs_on=["machine:hub"])
    r = _client().post(f"{API}/tasks/bulk", json={"ids": [other, tid], "assignee": "other"})
    assert r.status_code == 200, r.text
    res = {e["id"]: e for e in r.json()["results"]}
    assert res[other] == {"id": other, "ok": True}
    assert res[tid]["ok"] is False and res[tid]["holder"] == WORKER
    assert kb.get_task(hub.conn, other).assignee == "other"
    assert kb.get_task(hub.conn, tid).assignee == "default"


# --- regressions: no live remote lease, no refusal -------------------------------------------------------------

@pytest.mark.parametrize("lease", [_cleared, _own], ids=["board-cleared", "own-gateway"])
def test_patch_assignee_without_a_live_remote_lease_assigns_as_before(gws, svc, kills, lease):
    hub, tid = _remote(gws, svc)
    lease(hub, tid)
    r = _client().patch(f"{API}/tasks/{tid}", json={"assignee": "other"})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["assignee"] == "other"
    assert kb.get_task(hub.conn, tid).assignee == "other"


@pytest.mark.parametrize("lease", [_cleared, _own], ids=["board-cleared", "own-gateway"])
def test_bulk_assignee_without_a_live_remote_lease_assigns_as_before(gws, svc, kills, lease):
    hub, tid = _remote(gws, svc)
    lease(hub, tid)
    r = _client().post(f"{API}/tasks/bulk", json={"ids": [tid], "assignee": "other"})
    assert r.status_code == 200, r.text
    assert r.json()["results"] == [{"id": tid, "ok": True}]
    assert kb.get_task(hub.conn, tid).assignee == "other"


def test_patch_assignee_on_a_local_card_assigns_as_before(gws, svc, kills):
    hub, _tid = _remote(gws, svc)
    local = kb.create_task(hub.conn, title="queued here", assignee="default", runs_on=["machine:hub"])
    r = _client().patch(f"{API}/tasks/{local}", json={"assignee": "other"})
    assert r.status_code == 200, r.text
    assert kb.get_task(hub.conn, local).assignee == "other"
