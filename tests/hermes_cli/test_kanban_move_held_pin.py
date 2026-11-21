"""Capped queued cards may escape affirmative registry holds, never stale discovery."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_move as helpers

svc = helpers.svc
gws = helpers.gws


def held_registry(source):
    return {**helpers.LIVE, source: {"name": source, "state": "active", "labels": ["linux"],
                                    "pickup": "explicit", "hold": True}}


@pytest.mark.parametrize("source", ["worker-6", helpers.HUB])
def test_capped_held_pin_escape_is_audited_and_still_counts(gws, source):
    hub, _ = gws
    tid = helpers._capped_card_pinned_to(hub, source)
    registry = held_registry(source)
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="worker draining", machines=registry, me=helpers.HUB)
    assert res["ok"] and res["move_no"] == 4, res
    event = helpers._events(hub.conn, tid)[-1]
    assert event["held_pin"] == source and "dead_pin" not in event
    assert event["previous_runs_on"] == [f"machine:{source}"]
    assert registry[source]["hold"] is True
    again = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="again", machines=registry, me=helpers.HUB)
    assert again["reason_code"] == "move_limit", again


@pytest.mark.parametrize("outage", [False, True])
def test_held_pin_uses_real_registry_cache_and_refuses_failed_refresh(gws, monkeypatch, outage):
    hub, _ = gws
    tid = helpers._capped_card_pinned_to(hub, "worker-6")
    calls = []

    def fetch():
        calls.append(True)
        if outage and len(calls) > 1:
            raise OSError("registry offline")
        return {"me": helpers.HUB, "machines": list(held_registry("worker-6").values())}

    wn.set_registry_fetch(fetch)
    assert wn.registry()["machines"]["worker-6"]["hold"] is True
    monkeypatch.setitem(wn._REGISTRY, "at", -1e9)
    before = helpers._events(hub.conn, tid)
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="draining", me=helpers.HUB)
    assert len(calls) == 2
    if not outage:
        assert res["ok"] and res["move_no"] == 4, res
        return
    for result in (res, km.move_queued(hub.conn, tid, runs_on=["linux"], reason="retry", me=helpers.HUB)):
        assert result["reason_code"] == "registry_unavailable", result
    assert len(calls) == 2
    assert helpers._events(hub.conn, tid) == before
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == ["machine:worker-6"]


def test_held_pin_does_not_exempt_running_or_unpinned_cards(gws):
    hub, _ = gws
    tid = helpers._capped_card_pinned_to(hub, "worker-6")
    registry = held_registry("worker-6")
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    with pytest.raises(km.MoveRefused) as exc:
        km.check(hub.conn, tid, ["linux"], machines=registry, me=helpers.HUB, reason="r")
    assert exc.value.code == "move_limit"
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        wn.set_runs_on(hub.conn, tid, ["linux"])
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], machines=registry, me=helpers.HUB, reason="r")
    assert res["reason_code"] == "move_limit"


@pytest.mark.parametrize("previous,hold", [
    (["machine:worker-6", "machine:hub"], True),
    (["machine:worker-6"], False),
    (["machine:worker-6"], "true"),
    (["machine:worker-6"], 1),
])
def test_held_exception_requires_one_pin_and_boolean_hold(gws, previous, hold):
    hub, _ = gws
    tid = helpers._capped_card_pinned_to(hub, "worker-6")
    registry = held_registry("worker-6")
    registry["worker-6"]["hold"] = hold
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, previous)
    before = helpers._events(hub.conn, tid)
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="r", machines=registry, me=helpers.HUB)
    assert res["reason_code"] == "move_limit", res
    assert helpers._events(hub.conn, tid) == before


def test_held_rescue_preserves_target_and_ping_pong_guards(gws):
    hub, _ = gws
    tid = helpers._capped_card_pinned_to(hub, "worker-6")
    registry = held_registry("worker-6")
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-6"], reason="r", machines=registry, me=helpers.HUB)
    assert res["reason_code"] == "no_machine", res
    res = km.move_queued(hub.conn, tid, runs_on=[f"machine:{helpers.WORKER}"], host_reason=helpers.HR,
                         reason="r", machines=registry, me=helpers.HUB)
    assert res["reason_code"] == "ping_pong", res
