"""Read-only review and fault prediction before two-gateway pickup."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli import test_kanban_unroutable_pickup_review46 as eligibility
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml

svc = support.svc
gws = support.gws
imports = eligibility.imports
PIN = eligibility.PIN


def _review(hub, priority=100):
    hub.enter()
    tid = kb.create_task(hub.conn, title='Head check', assignee='default', priority=priority, runs_on=PIN)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    hub.round()
    return tid


@pytest.mark.parametrize('policy', ['model_routing:\n  enabled: false\n', 'terminal:\n  backend: local\n'])
def test_review_disabled_zero_imports_then_policy_recovery(gws, imports, policy):
    hub, worker = eligibility._setup(gws, policy)
    tid = _review(hub)
    worker.round()
    for _ in range(4):
        worker.tick(max_spawn=2)
        worker.round()
    assert imports.count(tid) == 0
    assert kb.list_runs(worker.conn, tid) == []
    assert kb.get_task(worker.conn, tid) is None
    source = kb.get_task(hub.conn, tid)
    assert source is not None and source.status == 'review'
    (worker.home / 'config.yaml').write_text(eligibility.SYNC + '    pickup: true\n' + _managed_yaml())
    worker.tick(max_spawn=2)
    assert imports.count(tid) == 1
    assert len(kb.list_runs(worker.conn, tid)) == 1


def test_review_standard_fallback_is_eligible_not_deep_exclusive(gws, imports):
    hub, worker = eligibility._setup(gws, eligibility._standard_only())
    tid = _review(hub)
    peer = support._hub_card(hub, title='Healthy peer', priority=1, runs_on=PIN)
    worker.round()
    worker.tick(max_spawn=2)
    assert imports.count(tid) == imports.count(peer) == 1
    assert len(kb.list_runs(worker.conn, tid)) == len(kb.list_runs(worker.conn, peer)) == 1


def test_one_resolver_fault_does_not_stop_healthy_pickup(gws, imports, monkeypatch):
    hub, worker = eligibility._setup(gws, _managed_yaml())
    bad = support._hub_card(hub, title='Bad candidate', priority=100, runs_on=PIN)
    peer = support._hub_card(hub, title='Healthy peer', priority=1, runs_on=PIN)
    worker.round()
    from hermes_cli import kanban_model_tier as tier
    real = tier.resolve_model_tier

    def faulty(conn, task, **kwargs):
        if task.id == bad:
            raise RuntimeError('candidate eligibility unavailable')
        return real(conn, task, **kwargs)
    monkeypatch.setattr(tier, 'resolve_model_tier', faulty)
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [peer]
    assert imports.count(peer) == 1
    assert imports.count(bad) == 0
    assert kb.list_runs(worker.conn, bad) == []
    monkeypatch.setattr(tier, 'resolve_model_tier', real)
    assert wn.pickup(worker.conn, budget=1) == [bad]
    assert imports.count(bad) == 1
