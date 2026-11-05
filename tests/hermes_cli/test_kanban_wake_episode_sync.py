"""Real sync feed/apply/pickup with an in-memory Board transport (not live proof)."""
import json
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_cross_gateway_parents as transport
from tests.hermes_cli import test_kanban_sync_copy_links as copies
from tests.hermes_cli import test_kanban_wake_episode as base

kanban_home = transport.kanban_home
live = transport.live
svc = transport.svc


def foreign(svc, home, stamp):
    tid = "t_episode_remote"
    transport._other(svc, "task", "tasks", tid, {
        "title": "remote probe", "status": "ready", "created_at": stamp,
        "created_on": transport.OTHER, "assignee": "worker", "workspace_kind": "scratch"})
    data = {"cmd": "exit 1", "first_at": stamp, "checks": 5,
            "last_stale_at": stamp + 48 * base.HOUR}
    event(svc, "stale", tid, data, stamp + 48 * base.HOUR)
    transport._round(home, svc)
    return tid


def event(svc, uid, tid, data, stamp):
    payload = {"_wake_episode_v1": data}
    if uid in {"reset", "park"}:
        payload["_wake_episode_barriers"] = {"foreign-reset": 1}
    transport._other(svc, uid, "task_events", uid, {
        "task_id": tid, "kind": "wake_check_stale" if uid == "stale" else "scheduled",
        "payload": json.dumps(payload), "created_at": stamp})


def test_pickup_retains_cooldown_and_publishes_next_stale(live, svc, kanban_home, monkeypatch):
    stamp = int(time.time())
    tid = foreign(svc, kanban_home, stamp)
    monkeypatch.setattr(time, "time", lambda: stamp + 49 * base.HOUR)
    monkeypatch.setattr(time, "time_ns", lambda: (stamp + 49 * base.HOUR) * 1_000_000_000)
    copies._import(live, tid)
    base.repark((live, tid, [stamp]))
    assert base.result((live, tid, [stamp]), 49) is None
    state = wc.get_state(live, tid)
    assert state["episode_first_at"] == stamp
    assert state["episode_checks"] == 6
    assert state["episode_last_stale_at"] == stamp + 48 * base.HOUR
    transport._round(kanban_home, svc)
    assert base.result((live, tid, [stamp]), 96) == base.STALE
    transport._round(kanban_home, svc)  # publish after this round's feed pull
    round_result = transport._round(kanban_home, svc)  # read back the published event
    shadow = transport.ksc.open_shadow(transport.BOARD, transport.ME)
    try:
        payloads = [json.loads(row[0]) for row in shadow.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, base.STALE))]
        assert any(p.get("checks") == 7 for p in payloads), (payloads, round_result, transport.sync.outbox(live))
    finally:
        shadow.close()


def test_feed_coalesced_reset_survives_following_unknown_same_probe(live, svc, kanban_home, monkeypatch):
    stamp = int(time.time())
    tid = foreign(svc, kanban_home, stamp)
    monkeypatch.setattr(time, "time", lambda: stamp + 49 * base.HOUR)
    monkeypatch.setattr(time, "time_ns", lambda: (stamp + 49 * base.HOUR) * 1_000_000_000)
    copies._import(live, tid)
    base.repark((live, tid, [stamp]))
    assert base.result((live, tid, [stamp]), 49) is None
    transport._round(kanban_home, svc)
    event(svc, "reset", tid, None, stamp + 50 * base.HOUR)
    event(svc, "park", tid, {"park_cmd": "exit 1"}, stamp + 51 * base.HOUR)
    transport._round(kanban_home, svc)
    assert kb.get_task(live, tid).status == "scheduled"
    assert base.result((live, tid, [stamp]), 96) is None
    assert wc.get_state(live, tid)["episode_first_at"] == stamp + 96 * base.HOUR
