"""A held creator strands its own default-placement cards when every other worker picks up ``explicit``.

t_01a1d4f2f591d595 (2026-11-13): t_01a1d49d50e4c656, default placement (``runs_on`` NULL) and created on worker-6,
woke ready while hub-worker-core-rollout held worker-6. Every pool worker runs ``kanban.sync.pickup: explicit``, which
copies only cards with ``runs_on``, so nothing claimed it for 14 min and no pickup-stall card was filed: the detector
watched only foreign cards pinned ``machine:<host>``. The rollout now moves such cards to the pool queue (state
``hub-worker-core-rollout``, which runs ``hermes kanban move <id> --pool --reason "<w> on rollout hold"`` on the held
worker); the detector files one alarm when they still wait past the stall threshold. Same fake-Board harness as
``test_kanban_worker_node``.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

HELD, FREE = "worker-6", "worker-1"
DETECTOR = "pickup-stall-detector"
POOL = "pool:worker"
LABELS = ["linux", "profile:default", "capability:terminal", "capability:file", "capability:kanban", POOL]


def _machines(t0: float, *, free_pickup="explicit", held=True) -> dict:
    hold = {"hold": True, "hold_owner": "rollout:worker-core-rollout@hub", "hold_at": int(t0 * 1000),
            "hold_reason": "worker-core-rollout: aaaaaaaaaaaa -> bbbbbbbbbbbb; drain only; release by 23:59Z"} \
        if held else {"hold": False}
    return {
        "hub": {"name": "hub", "state": "active", "labels": ["linux"], "pinned_only": False, "max_card_runs": None,
                "hold": False},
        HELD: {"name": HELD, "state": "active", "labels": LABELS, "pinned_only": False, "max_card_runs": 10,
               "pickup": "explicit", **hold},
        FREE: {"name": FREE, "state": "active", "labels": LABELS, "pinned_only": False, "max_card_runs": 10,
               "pickup": free_pickup, "hold": False, "metrics": {"active_card_runs": 3}},
    }


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """worker-6 (held) and worker-1, both ``pickup: explicit``, on one fake Board."""
    svc = FakeService(lease_runs=True)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    t0 = time.time()
    state = {"machines": _machines(t0)}
    monkeypatch.setattr(support, "MACHINES", state["machines"])
    sync.consume_dispatcher_wake()
    held = support.Gateway(tmp_path, HELD, svc, monkeypatch, pickup="explicit")
    free = support.Gateway(tmp_path, FREE, svc, monkeypatch, pickup="explicit")

    def registry(machines, *, seen_ms=None):
        """Serve ``machines``, stamped seen now (or at ``seen_ms``: a sweep at a simulated time sees the registry
        as fresh only when ``last_seen`` is near that time)."""
        monkeypatch.setattr(support, "MACHINES", machines)
        if seen_ms is None:
            wn.set_registry_fetch(lambda: {"me": None, "machines": support.seen_now(machines.values())})
        else:
            wn.set_registry_fetch(lambda: {"me": None, "machines": [{"last_seen": seen_ms, **m}
                                                                    for m in machines.values()]})

    yield held, free, t0, registry
    for g in (held, free):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _own_card(gw, title="lane owner"):
    gw.enter()
    tid = kb.create_task(gw.conn, title=title, assignee="default")
    gw.round()
    return tid


def _repairs(conn):
    return conn.execute("SELECT * FROM tasks WHERE created_by = ? ORDER BY created_at, id", (DETECTOR,)).fetchall()


def test_t1_a_pool_move_of_the_held_creators_default_card_lets_an_explicit_worker_take_it(pair):
    """T1, core half: the exact command hub-worker-core-rollout runs on the held worker
    (``hermes kanban move <id> --pool --reason "<w> on rollout hold"``) leaves runs_on [pool:worker] and a
    ``moved`` event, and the other explicit-pickup worker's pickup then copies the card (it skipped it before)."""
    held, free, _t0, _reg = pair
    tid = _own_card(held)
    free.round()
    free.enter()
    assert wn.pickup(free.conn, budget=4) == []  # explicit pickup skips a default-placement card: stranded
    held.enter()
    from hermes_cli import kanban as kcli
    out = json.loads(kcli.run_slash(f'move {tid} --pool --reason "{HELD} on rollout hold" --json'))
    assert out["ok"], out
    row = held.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert json.loads(row["runs_on"]) == [POOL]
    [moved] = [e for e in kb.list_events(held.conn, tid) if e.kind == "moved"]
    assert moved.payload["reason"] == f"{HELD} on rollout hold" and moved.payload["runs_on"] == [POOL]
    assert FREE in moved.payload["matches"] and HELD not in moved.payload["matches"]
    held.round()
    free.round()
    free.enter()
    assert wn.pickup(free.conn, budget=4) == [tid]


def test_t3_one_alarm_after_the_threshold_for_a_default_card_only_the_held_creator_may_run(pair):
    held, _free, t0, _reg = pair
    tid = _own_card(held)
    held.enter()
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.sweep(held.conn, now=t0 + kps.stall_seconds() - 30) == []  # held under the threshold: nothing yet
    assert not _repairs(held.conn)
    later = t0 + kps.stall_seconds() + 60
    [first] = kps.sweep(held.conn, now=later)
    assert kps.sweep(held.conn, now=later + 60) == [first]  # one alarm per incident: reused
    [alarm] = _repairs(held.conn)
    assert f"held {HELD}" in alarm["title"] and tid in alarm["title"]
    assert f"- {tid} (" in alarm["body"] and "default placement" in alarm["body"]
    assert alarm["human_request"] is None
    assert json.loads(alarm["runs_on"]) == [f"machine:{FREE}"]  # never on the held machine
    assert alarm["idempotency_key"].startswith(f"pickup-stall:{HELD}:held-default:")
    from hermes_cli import kanban_pickup_stall_repin as repin
    held.round()  # the alarm runs elsewhere: it and the target are read from the Board replica, as there
    held.round()  # push, then pull the Board's copy back into the replica
    rep = wn.open_replica(held.conn)
    assert rep is not None
    try:
        alarm = rep.execute("SELECT * FROM tasks WHERE id = ?", (first,)).fetchone()
        target = rep.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        machines = {n: {**m, "last_seen": int(time.time() * 1000)} for n, m in support.MACHINES.items()}
        assert alarm["created_on"] == HELD and target["created_on"] == HELD
        assert repin.scope_refusal(rep, alarm, target, [POOL], machines=machines,
                                   now_ms=int(time.time() * 1000)) is None
        # A foreign default card (created elsewhere) is not in a held-default alarm's scope.
        other = {**dict(target), "created_on": FREE}
        assert "no longer pinned" in str(repin.scope_refusal(rep, alarm, other, [POOL], machines=machines,
                                                             now_ms=int(time.time() * 1000)))
    finally:
        rep.close()


def test_t3_no_alarm_when_another_machine_can_claim_the_card(pair):
    held, _free, t0, registry = pair
    from hermes_cli import kanban_pickup_stall as kps
    later = t0 + kps.stall_seconds() + 60
    registry(_machines(t0, free_pickup="all"))  # worker-1 copies default-placement cards: not stranded
    _own_card(held)
    held.enter()
    registry(_machines(t0, free_pickup="all"), seen_ms=int(later * 1000))  # fresh at the sweep, not draining
    assert kps.sweep(held.conn, now=later) == []
    assert not _repairs(held.conn)


@pytest.mark.parametrize("dead", ["stale", "draining"])
def test_a_stale_or_draining_default_taker_does_not_silence_the_alarm(pair, dead):
    """PR #2264 review 5892044674: a non-explicit machine that claims nothing (last_seen older than
    ``kanban_pickup_stall.FRESH_SECONDS``, or ``metrics.drain_active``) is not a taker, as ``alarm_placement``
    judges it; counting it left a card no one could claim without an alarm."""
    held, _free, t0, registry = pair
    from hermes_cli import kanban_pickup_stall as kps
    later = t0 + kps.stall_seconds() + 60
    tid = _own_card(held)
    held.enter()
    machines = _machines(t0, free_pickup="all")
    if dead == "stale":
        machines[FREE]["last_seen"] = int((later - kps.FRESH_SECONDS - 60) * 1000)
    else:
        machines[FREE]["metrics"] = {**machines[FREE]["metrics"], "drain_active": True}
    registry(machines, seen_ms=int(later * 1000))
    [alarm_id] = kps.sweep(held.conn, now=later)
    [alarm] = _repairs(held.conn)
    assert alarm["id"] == alarm_id and f"- {tid} (" in alarm["body"]


def test_no_alarm_once_the_card_left_default_placement_or_the_hold_ended(pair):
    held, _free, t0, registry = pair
    tid = _own_card(held)
    held.enter()
    from hermes_cli import kanban_pickup_stall as kps
    later = t0 + kps.stall_seconds() + 60
    registry(_machines(t0, held=False))
    assert kps.sweep(held.conn, now=later) == []
    registry(_machines(t0))
    from hermes_cli import kanban as kcli
    assert json.loads(kcli.run_slash(f'move {tid} --pool --reason "{HELD} on rollout hold" --json'))["ok"]
    assert kps.sweep(held.conn, now=later) == []
    assert not _repairs(held.conn)


@pytest.mark.parametrize("hold,listed", [("yes", True), ("", False)])
def test_stranded_reads_the_hold_by_truthiness_like_pickup(pair, hold, listed):
    """Pickup's ``eligible()`` treats any truthy ``hold`` as held (PR #2526). ``stranded()`` must read it the same
    way: a ``"yes"`` hold strands the default card (the alarm fires), a falsy ``""`` hold strands nothing. An
    ``is not True`` read listed nothing for ``"yes"``, so the alarm never fired while pickup held the machine."""
    held, _free, t0, _reg = pair
    tid = _own_card(held)
    held.enter()
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli import kanban_pickup_stall_held as kpsh
    machines = _machines(t0)
    machines[HELD]["hold"] = hold
    assert (wn.eligible({"runs_on": None, "created_on": HELD}, HELD, machines) is not None) is listed
    later_ms = int((t0 + kps.stall_seconds() + 60) * 1000)
    got = kpsh.stranded(held.conn, HELD, machines, now_ms=later_ms, stall_s=kps.stall_seconds())
    assert [c["id"] for c in got] == ([tid] if listed else [])


def test_a_worktree_card_is_never_listed(pair):
    held, _free, t0, _reg = pair
    held.enter()
    kb.create_task(held.conn, title="bound", assignee="default", workspace_kind="dir", workspace_path=str(held.home / "ws"))
    from hermes_cli import kanban_pickup_stall as kps
    assert kps.sweep(held.conn, now=t0 + kps.stall_seconds() + 60) == []
