"""A ``hold`` repair needs the hold to still be in the live registry, and the producer closes a host-wide repair
whose cards have all left ``ready``. [daveey]

t_01a1d5b42df173d3 (2026-11-13): worker-6 filed t_01a1d59589cb8efc "Pickup held on worker-6" at 15:45:09Z from
``hold`` skips last recorded at 15:43:45Z, while the registry entry its own body quoted already read
``hold=False``. The card it named ran at 15:55Z; the alarm stayed ``ready`` until it was archived by hand at 16:18Z.
The same shape was filed at 07:18Z on 11-12 (t_01a1d3c595358fa2) and closed "No move needed".
Same fake-Board two-gateway harness as ``test_kanban_pickup_stall``.
"""
from __future__ import annotations

import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_pickup_stall as base
from tests.hermes_cli import test_kanban_worker_node as support

WORKER = support.WORKER
svc = support.svc
gws = support.gws


def _registry(**fields):
    machines = {**support.MACHINES, WORKER: {**support.MACHINES[WORKER], **fields}}
    wn.set_registry_fetch(lambda: {"me": WORKER, "machines": support.seen_now(machines.values())})
    wn._REGISTRY.update(at=None, value=None, retry=False)


def _held_cards(gws, **hold):
    """Two cards pinned to the worker, skipped as ``hold`` by a pickup pass while the registry holds it, and aged
    past the stall threshold. Returns (hub, worker, [older, newer])."""
    hub, worker = gws
    a = base._pinned(hub, title="a")
    b = base._pinned(hub, title="b")
    worker.round()
    worker.enter()
    _registry(hold=True, **hold)
    wn.pickup(worker.conn, budget=4)
    base._age(worker, a, 900)
    base._age(worker, b, 700)
    return hub, worker, [a, b]


def _lift(worker):
    """The hold ends: the registry entry is active, unheld and advertises pickup again."""
    worker.enter()
    _registry(hold=False, hold_owner=None, hold_reason=None, hold_at=None)
    entry = wn.registry()["machines"][WORKER]
    assert entry["state"] == "active" and entry["hold"] is False and wn.advertises_pickup(entry)


def test_hold_skips_from_a_lifted_hold_file_no_hold_repair(gws, monkeypatch):
    from hermes_cli import kanban_pickup_stall as kps
    _, worker, cards = _held_cards(gws, hold_owner="ops@hub", hold_at=int(time.time() * 1000),
                                   hold_reason="disk migration")
    assert all(kps.latest(worker.conn, t)["reason"] == "hold" for t in cards)
    _lift(worker)
    assert kps.sweep(worker.conn) == []  # the newest skip still says hold, the live registry does not
    assert not base._repairs(worker.conn)
    # Still unpicked after the lift: the next pass records why, and the alarm is about that reason.
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: name != "default"))
    worker.tick()
    worker.tick()
    assert all(kps.latest(worker.conn, t)["reason"] == "profile_ineligible" for t in cards)
    repairs = base._repairs(worker.conn)
    assert repairs and not any("held" in r["title"].lower() for r in repairs)
    assert {t for t in cards if any(t in r["title"] and "profile_ineligible" in r["title"] for r in repairs)} \
        == set(cards)


def test_a_real_operator_hold_still_files_exactly_one_card_per_host(gws):
    from hermes_cli import kanban_pickup_stall as kps
    _, worker, cards = _held_cards(gws, hold_owner="ops@hub", hold_at=int(time.time() * 1000),
                                   hold_reason="disk migration")
    kps.sweep(worker.conn)
    kps.sweep(worker.conn)
    [card] = base._repairs(worker.conn)
    assert "held" in card["title"].lower() and cards[0] in card["title"] and cards[1] in card["body"]


def _states(worker, tid):
    return [c.body for c in kb.list_comments(worker.conn, tid) if c.author == base.DETECTOR]


def test_an_open_hold_repair_is_closed_once_every_card_it_names_left_ready(gws):
    from hermes_cli import kanban_pickup_stall as kps
    hub, worker, (a, b) = _held_cards(gws, hold_owner="ops@hub", hold_at=int(time.time() * 1000),
                                      hold_reason="disk migration")
    kps.sweep(worker.conn)
    [repair] = base._repairs(worker.conn)
    # Only one card leaves ready: the repair still has work.
    hub.enter()
    kb.complete_task(hub.conn, a, summary="ran elsewhere")
    hub.round()
    worker.round()
    worker.enter()
    _registry(hold=True, hold_owner="ops@hub", hold_at=int(time.time() * 1000), hold_reason="disk migration")
    kps.sweep(worker.conn)
    assert kb.get_task(worker.conn, repair["id"]).status == "ready"
    # The other card is withdrawn on the Board while the hold still stands: nothing is left for the repair.
    hub.enter()
    kb.archive_task(hub.conn, b)
    hub.round()
    worker.round()
    worker.enter()
    _registry(hold=True, hold_owner="ops@hub", hold_at=int(time.time() * 1000), hold_reason="disk migration")
    kps.sweep(worker.conn)
    assert kb.get_task(worker.conn, repair["id"]).status == "archived"
    [note] = [c for c in _states(worker, repair["id"]) if "left ready" in c]
    assert f"{a}: done on the Board" in note and f"{b}: archived on the Board" in note
    # A later stall is a new incident: a fresh card, not the closed one reused.
    c = base._pinned(hub, title="c")
    worker.round()
    worker.enter()
    _registry(hold=True, hold_owner="ops@hub", hold_at=int(time.time() * 1000), hold_reason="disk migration")
    wn.pickup(worker.conn, budget=4)
    base._age(worker, c, 900)
    kps.sweep(worker.conn)
    fresh = [r for r in base._repairs(worker.conn) if r["id"] != repair["id"]]
    assert len(fresh) == 1 and c in fresh[0]["title"] and fresh[0]["status"] == "ready"


def test_an_open_hold_repair_is_closed_when_the_hold_lifted_and_its_cards_still_wait(gws, monkeypatch):
    from hermes_cli import kanban_pickup_stall as kps
    _, worker, (a, b) = _held_cards(gws, hold_owner="ops@hub", hold_at=int(time.time() * 1000),
                                    hold_reason="disk migration")
    kps.sweep(worker.conn)
    [repair] = base._repairs(worker.conn)
    _lift(worker)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: name != "default"))
    worker.tick()  # closes the hold repair; pickup records the next reason
    assert kb.get_task(worker.conn, repair["id"]).status == "archived"
    [note] = [c for c in _states(worker, repair["id"]) if "hold" in c and "lifted" in c]
    assert f"{a}: ready" in note and f"{b}: ready" in note
    worker.tick()  # the cards are now judged by that reason, not hidden behind the closed hold card
    fresh = [r for r in base._repairs(worker.conn) if r["id"] != repair["id"]]
    assert {t for t in (a, b) if any(t in r["title"] and "profile_ineligible" in r["title"] for r in fresh)} \
        == {a, b}


def test_a_claimed_repair_is_left_to_its_run(gws):
    from hermes_cli import kanban_pickup_stall as kps
    _, worker, _cards = _held_cards(gws, hold_owner="ops@hub", hold_at=int(time.time() * 1000),
                                    hold_reason="disk migration")
    kps.sweep(worker.conn)
    [repair] = base._repairs(worker.conn)
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET status = 'running', claim_lock = 'x' WHERE id = ?", (repair["id"],))
    _lift(worker)
    kps.sweep(worker.conn)  # (no dispatcher tick: it would reclaim this fake claim)
    assert kb.get_task(worker.conn, repair["id"]).status == "running"
