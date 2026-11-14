"""Move preflight uses worker requirement labels without changing create's contract."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_runs_on_capabilities as roc
from tests.hermes_cli import test_kanban_move as helpers

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

svc = helpers.svc
gws = helpers.gws


def _card(hub, *, requires=(), skills=()):
    hub.enter()
    tid = kb.create_task(hub.conn, title="queued", assignee="default", requires=[])
    # Existing cards can have remote-only pins/needs; do not invoke local create validation.
    hub.conn.execute("UPDATE tasks SET requires = ?, skills = ? WHERE id = ?",
                     (json.dumps(list(requires)), json.dumps(list(skills)), tid))
    return tid


def _machines(*labels):
    return {"worker-1": {"state": "active", "pickup": "all", "labels": list(labels)}}


def test_queued_move_can_target_self_without_pickup(gws):
    hub, _ = gws
    tid = _card(hub, requires=["terminal"])
    result = km.move_queued(hub.conn, tid, runs_on=["machine:hub"], host_reason=HR, reason="keep local",
                            machines={"hub": {"state": "active", "pickup": False}}, me="hub")
    assert result["ok"] and result["matches"] == ["hub"], result
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == ["machine:hub"]
    assert helpers._events(hub.conn, tid)[0]["queued"] is True


@pytest.mark.parametrize("needs,pins,missing", [
    (["host_repo"], [], "capability:host_repo"),
    ([], ["five-whys"], "skill:five-whys"),
    ([], ["plugin:custom"], "skill:plugin:custom"),
])
@pytest.mark.parametrize("running", [False, True])
def test_move_refuses_missing_worker_labels_without_writes(gws, monkeypatch, needs, pins, missing, running):
    hub, _ = gws
    if running:
        tid, _ = helpers._running_hub_card(hub)
        hub.conn.execute("UPDATE tasks SET requires = ?, skills = ? WHERE id = ?",
                         (json.dumps(needs), json.dumps(pins), tid))
    else:
        tid = _card(hub, requires=needs, skills=pins)
    monkeypatch.setattr(roc, "_remote_tools", lambda name: None)
    before = dict(hub.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    result = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="move", checkpoint="saved",
                          machines=_machines("profile:default", "capability:terminal"), me="hub") if running else (
        km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="move",
                       machines=_machines("profile:default", "capability:terminal"), me="hub"))
    assert result.get("reason_code") == "capability_mismatch", result
    assert missing in result["error"] and "worker-1" in result["error"]
    assert dict(hub.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()) == before
    assert helpers._events(hub.conn, tid) == []


def test_move_requires_one_target_with_both_labels_and_profile_tools(gws, monkeypatch):
    hub, _ = gws
    tid = _card(hub, requires=["terminal", "host_repo"], skills=["five-whys", "plan"])
    machines = _machines("capability:host_repo", "skill:five-whys", "skill:plan")
    machines["other"] = {"state": "active", "labels": []}
    monkeypatch.setattr(roc, "_remote_tools", lambda name: {"default": [] if name == "worker-1" else ["terminal"]})
    with pytest.raises(km.MoveRefused) as exc:
        km.check(hub.conn, tid, ["linux"], machines={n: {**v, "labels": v["labels"] + ["linux"]}
                                                  for n, v in machines.items()}, me="hub")
    assert exc.value.code == "capability_mismatch"
    monkeypatch.setattr(roc, "_remote_tools", lambda name: {"default": ["terminal"]})
    result = km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="capable target",
                            machines=machines, me="hub")
    assert result["ok"] and result["matches"] == ["worker-1"], result


def test_queued_self_still_checks_local_profile_tools(gws, monkeypatch):
    hub, _ = gws
    tid = _card(hub, requires=["terminal"])
    monkeypatch.setattr(roc, "_local_tools", lambda assignee: set())
    result = km.move_queued(hub.conn, tid, runs_on=["machine:hub"], host_reason=HR, reason="local",
                            machines={"hub": {"state": "active"}}, me="hub")
    assert result.get("reason_code") == "capability_mismatch", result


@pytest.mark.parametrize("missing", ["capability:host_repo", "skill:five-whys", "skill:plan"])
def test_every_required_label_must_be_on_the_same_target(gws, monkeypatch, missing):
    hub, _ = gws
    tid = _card(hub, requires=["host_repo"], skills=["five-whys", "plan"])
    labels = {"capability:host_repo", "skill:five-whys", "skill:plan"}
    machines = _machines(*(labels - {missing}))
    monkeypatch.setattr(roc, "_remote_tools", lambda name: None)
    result = km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="partial",
                            machines=machines, me="hub")
    assert result.get("reason_code") == "capability_mismatch" and missing in result["error"], result
    machines["capable"] = {"state": "active", "labels": sorted(labels)}
    for entry in machines.values():
        entry["labels"].append("linux")
    plan = km.check(hub.conn, tid, ["linux"], machines=machines, me="hub")
    assert plan["matches"] == ["capable"]


@pytest.mark.parametrize("entry", [{"state": "inactive"}, {"state": "active", "hold": True}, {}])
def test_queued_self_must_still_be_active_and_unheld(gws, entry):
    hub, _ = gws
    tid = _card(hub)
    result = km.move_queued(hub.conn, tid, runs_on=["machine:hub"], host_reason=HR, reason="local",
                            machines={"hub": entry}, me="hub")
    assert result.get("reason_code") == "no_machine", result


@pytest.mark.parametrize('missing', [None, 'host_repo', 'skill'])
def test_queued_return_uses_local_inventory_not_worker_labels(gws, monkeypatch, missing):
    from contextlib import nullcontext
    from hermes_cli import kanban_capabilities as caps
    hub, _ = gws
    tid = _card(hub, requires=['host_repo'], skills=['local-skill'])
    monkeypatch.setattr(caps, '_profile_capabilities', lambda assignee: set() if missing == 'host_repo' else {'host_repo'})
    monkeypatch.setattr(caps, '_profile_policy', lambda assignee: nullcontext())
    monkeypatch.setattr(caps, '_skill_frontmatter', lambda pin: None if missing == 'skill' else {})
    result = km.move_queued(hub.conn, tid, runs_on=['machine:hub'], host_reason=HR, reason='return home',
                            machines={'hub': {'state': 'active'}}, me='hub')
    assert result['ok'] is (missing is None), result


@pytest.mark.parametrize('remote', [False, True])
def test_missing_local_profile_does_not_veto_capable_remote(gws, monkeypatch, remote):
    hub, _ = gws
    tid = _card(hub, requires=['host_repo'], skills=['plan'])
    hub.conn.execute("UPDATE tasks SET assignee = 'remote-only' WHERE id = ?", (tid,))
    machines = {'hub': {'state': 'active', 'labels': ['linux']}}
    if remote:
        machines.update(_machines('linux', 'capability:host_repo', 'skill:plan'))
    result = km.move_queued(hub.conn, tid, runs_on=['linux'], reason='remote profile',
                            machines=machines, me='hub')
    assert result['ok'] is remote, result
    if remote:
        assert result['matches'] == ['worker-1']
    else:
        assert result['reason_code'] == 'capability_mismatch'
