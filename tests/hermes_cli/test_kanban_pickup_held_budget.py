"""A card the Board shows held by a respawn guard never takes the pickup budget (``kanban_pickup_held``).

worker-1 2026-11-12 11:40-11:57Z: four guarded hub cards at the top of the dispatch order filled the 3-card pickup
batch every tick, so six cards pinned to ``machine:worker-1`` waited ``budget_exhausted`` (t_01a1cf9fa5c6616f)."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pickup_held as kph
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_db_events import append_event
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli.test_kanban_pickup_stall import alarm_worker_pool

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]


@pytest.fixture(autouse=True)
def _fresh_cache():
    kph.reset()
    yield
    kph.reset()


@pytest.fixture
def guard(monkeypatch):
    """``check_respawn_guard`` holding the ids in ``held`` on this machine too."""
    held: dict[str, str] = {}
    real = kbd.check_respawn_guard
    monkeypatch.setattr(kbd, "check_respawn_guard",
                        lambda conn, tid, lane="ready": held.get(tid) or real(conn, tid, lane=lane))
    return held


@pytest.fixture
def imports(monkeypatch):
    seen: list[str] = []
    real = wn.import_cards

    def counting(conn, replica, ids):
        done = real(conn, replica, ids)
        seen.extend(done)
        return done
    monkeypatch.setattr(wn, "import_cards", counting)
    return seen


def _held_on_board(hub, priority, reason="subscription_quota_reset", runs_on=()):
    """A hub card another machine's dispatcher holds: its ``respawn_guarded`` event is in the Board history."""
    hub.enter()
    tid = kb.create_task(hub.conn, title=f"held {priority}", assignee="default", priority=priority,
                         runs_on=list(runs_on))
    with write_txn(hub.conn):
        append_event(hub.conn, tid, "respawn_guarded", {"reason": reason})
    hub.round()
    return tid


def _skip(conn, tid):
    from hermes_cli import kanban_pickup_stall as kps
    latest = kps.latest(conn, tid)
    return latest["reason"] if latest else None


def _pickup(worker, budget):
    worker.round()
    worker.enter()
    return wn.pickup(worker.conn, budget=budget)


def test_guarded_cards_do_not_fill_the_batch_and_a_pinned_card_is_picked(gws, guard, imports):
    hub, worker = gws
    held = [_held_on_board(hub, p) for p in (20801, 1000, 1000, 360)]
    pinned = support._hub_card(hub, title="pinned", priority=0, runs_on=PIN)
    guard.update({tid: "subscription_quota_reset" for tid in held})

    got = _pickup(worker, 3)
    assert pinned in got
    assert set(held) <= set(got)  # copied outside the budget: the local guard decides on its own copy
    assert _skip(worker.conn, pinned) != "budget_exhausted"

    worker.tick(max_spawn=3)  # the local dispatcher holds the four and starts the pinned card
    assert kb.get_task(worker.conn, pinned).status == "running"
    for _ in range(3):  # the held copies are kept, never copied again
        _pickup(worker, 2)
        worker.tick(max_spawn=2)
    assert all(imports.count(tid) == 1 for tid in held)
    assert all(kb.get_task(worker.conn, tid).status == "ready" for tid in held)


def test_an_unkept_held_copy_is_not_reimported_every_tick(gws, imports, monkeypatch):
    hub, worker = gws
    tid = _held_on_board(hub, 900, "active_pr")
    clock = [1000.0]
    monkeypatch.setattr(kph.time, "monotonic", lambda: clock[0])

    assert _pickup(worker, 1) == [tid]
    worker.enter()
    assert wn.evict(worker.conn) == [tid]  # this machine's dispatcher never held it
    for _ in range(3):
        assert _pickup(worker, 1) == []
    assert imports.count(tid) == 1
    clock[0] += kph.RECHECK_SECONDS  # the recheck window passed: copy it again to re-check the guard here
    assert _pickup(worker, 1) == [tid]


def test_a_new_hold_is_rechecked_at_once(gws, imports):
    hub, worker = gws
    tid = _held_on_board(hub, 900, "active_pr")
    assert _pickup(worker, 1) == [tid]
    worker.enter()
    assert wn.evict(worker.conn) == [tid]
    hub.enter()
    with write_txn(hub.conn):
        append_event(hub.conn, tid, "unblocked", {})
        append_event(hub.conn, tid, "respawn_guarded", {"reason": "recent_success"})
    hub.round()
    assert _pickup(worker, 1) == [tid]


def test_the_held_imports_per_tick_are_bounded(gws, monkeypatch):
    hub, worker = gws
    monkeypatch.setattr(kph, "FREE_PER_TICK", 2)
    held = [_held_on_board(hub, 900 - i) for i in range(3)]
    plain = support._hub_card(hub, title="plain", priority=0, runs_on=PIN)
    # Score order spans pinned and pool cards; holds still consume only the free allowance.
    got = _pickup(worker, 2)
    assert got == held[:2] + [plain]
    assert held[2] not in got
    assert _skip(worker.conn, plain) != "budget_exhausted"


def test_a_capped_tick_reports_a_held_pinned_card_as_held(gws):
    hub, worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="pinned held", assignee="default", priority=0, runs_on=PIN)
    with write_txn(hub.conn):
        append_event(hub.conn, tid, "respawn_guarded", {"reason": "active_pr"})
    hub.round()
    worker.round()
    worker.enter()
    wn.note_capped(worker.conn)
    assert _skip(worker.conn, tid) == "guard_held"


def test_a_stalled_guard_held_card_does_not_file_the_registry_hold_repair(gws):
    """A Board guard hold is not this machine's registry hold: no "Pickup held on <own>" / "do not lift it" card."""
    from hermes_cli import kanban_pickup_stall as kps
    from tests.hermes_cli.test_kanban_pickup_stall import _age, _repairs
    hub, worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="pinned held", assignee="default", priority=0, runs_on=PIN)
    with write_txn(hub.conn):
        append_event(hub.conn, tid, "respawn_guarded", {"reason": "active_pr"})
    hub.round()
    worker.round()
    worker.enter()
    wn.note_capped(worker.conn)
    assert _skip(worker.conn, tid) == "guard_held"
    _age(worker, tid, 900)
    kps.sweep(worker.conn)
    [repair] = _repairs(worker.conn)
    assert tid in repair["title"] and "respawn guard" in repair["title"] and "active_pr" in repair["title"]
    assert "Pickup held on" not in repair["title"]
    assert "do not lift it" not in repair["body"] and "not on a registry hold" in repair["body"]


def test_an_unreadable_board_hold_takes_the_budget_as_before(gws, monkeypatch):
    hub, worker = gws
    tid = _held_on_board(hub, 900, runs_on=PIN)  # pinned here too: such cards go first (t_01a1d95eabbd2b9d)
    pinned = support._hub_card(hub, title="pinned", priority=0, runs_on=PIN)

    def boom(*a, **kw):
        raise RuntimeError("bad history")
    monkeypatch.setattr("hermes_cli.kanban_db_dispatch_guard_notice.guard_hold_reason", boom)
    assert _pickup(worker, 1) == [tid]
    assert _skip(worker.conn, pinned) == "budget_exhausted"
