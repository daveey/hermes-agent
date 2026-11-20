"""Pickup spends no budget on a review the local dispatcher would defer on a reviewer wall.

PR #3783 review: three walled review cards ahead of a pinned eligible card filled every pickup
batch. Each tick copied them, local dispatch held them, the next tick evicted and copied them
again (16 imports in 8 ticks at max_spawn=2), and the pinned card never copied.
"""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_llm_pacing_dispatch as pacing
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_review_model as review
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]
RESET = 1_800_007_200


def _imports(monkeypatch):
    seen: list[str] = []
    real = wn.import_cards

    def counting(conn, replica, ids):
        done = real(conn, replica, ids)
        seen.extend(done)
        return done
    monkeypatch.setattr(wn, "import_cards", counting)
    return seen


def _wall(monkeypatch):
    monkeypatch.setattr(tier, "load_tier_config", lambda: {"enabled": True})
    monkeypatch.setattr(review, "implementer_models", lambda *_a: {"gpt-6-astra", "gpt-6.1-sol"})
    monkeypatch.setattr(review, "independent_routes", lambda *_a, **_kw: iter([
        ("claude-fable-5-2", "anthropic"), ("claude-opus-5-5", "anthropic")]))
    monkeypatch.setattr(review, "registry_refusal", lambda *_a: None)
    monkeypatch.setattr(walls, "active_walls", lambda **_kw: {"anthropic": RESET})
    monkeypatch.setattr(walls, "live_quota_wall", lambda *_a: None)
    monkeypatch.setattr(pacing, "held_pools", lambda *_a: {})


def _review(hub, *, title, priority, runs_on):
    hub.enter()
    tid = kb.create_task(hub.conn, title=title, assignee="default", priority=priority, runs_on=runs_on)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    hub.round()
    return tid


def _status(conn, tid):
    t = kb.get_task(conn, tid)
    return t.status if t else None


def test_walled_review_cards_do_not_starve_a_pinned_card(gws, monkeypatch):
    _wall(monkeypatch)
    hub, worker = gws
    imports = _imports(monkeypatch)
    walled = [_review(hub, title=f"Independent exact-head review {i}", priority=1000 - i,
                      runs_on=["linux"] if i % 2 else PIN) for i in range(3)]
    pinned = support._hub_card(hub, title="pinned, eligible", priority=0, runs_on=PIN)
    worker.round()
    imports.clear()
    for _ in range(8):
        worker.round()
        worker.tick(max_spawn=2)
    assert imports == [pinned]
    assert _status(worker.conn, pinned) == "running"
    assert all(_status(worker.conn, tid) is None for tid in walled)


def test_a_failed_review_wall_check_copies_as_before(gws, monkeypatch):
    _wall(monkeypatch)
    hub, worker = gws
    tid = _review(hub, title="Independent exact-head review", priority=10, runs_on=PIN)
    worker.round()
    worker.enter()
    from hermes_cli import kanban_review_admission as admission

    def boom(*a, **kw):
        raise RuntimeError("config unreadable")
    monkeypatch.setattr(admission, "pickup_hold", boom, raising=False)
    assert wn.pickup(worker.conn, budget=1) == [tid]


def test_an_unwalled_review_is_copied(gws, monkeypatch):
    _wall(monkeypatch)
    monkeypatch.setattr(walls, "active_walls", lambda **_kw: {})
    hub, worker = gws
    tid = _review(hub, title="Independent exact-head review", priority=10, runs_on=PIN)
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]
