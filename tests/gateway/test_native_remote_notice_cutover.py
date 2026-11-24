"""Real replica->gate->FIFO->transcript custody, including OFF and unloaded handoff."""
import asyncio


import pytest

from gateway import kanban_remote_owner as owner
from gateway.kanban_notification_schedule import BoundActions, PassReport
from tests.gateway import test_kanban_remote_owner_delivery as helpers
from tui_gateway import notification_loaded_f as loaded
from tui_gateway import notification_gateway_sources as sources
from tui_gateway import kanban_remote_notices as notices

pytestmark = pytest.mark.platforms('linux')


env = helpers.env
TID = 't_01a20796629b4566'


@pytest.mark.parametrize('link_arrival', [False, True])
def test_native_remote_arrival_ack_failure_off_and_unloaded_exactly_once(env, monkeypatch, link_arrival):
    hub, runner, authority, fifo, db = env
    authority.sessions[helpers.CHAT] = helpers._Live(helpers.CHAT)
    helpers._busy(db, fifo)
    card = helpers._done(TID)
    initial = {**card, 'sessions': []} if link_arrival else {**card, 'status': 'running', 'completion_notice': None}
    helpers._publish(hub, [initial])
    monkeypatch.setattr(sources, 'inventory', lambda: ((hub / 'hub.db',), ()))
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    scheduler = loaded.Scheduler()
    monkeypatch.setattr(loaded, '_owner', scheduler)
    gate = BoundActions(lambda: None, native=True)

    def tick(now, legacy=False):
        if not gate.due(now, legacy_due=legacy):
            return []
        report = PassReport()
        result = asyncio.run(owner.tick(runner, report=report))
        gate.settled(report, now, 10)
        return result

    try:
        assert tick(0) == []
        scheduler.step()
        assert scheduler.gateway_sources.ready
        assert tick(0) == []
        scheduler.event.clear()
        helpers._publish(hub, [card])
        assert scheduler.event.wait(5), 'actual native remote commit never reached the shared owner'
        scheduler.step()
        assert len(tick(10)) == 1
        # Same canonical output key as the legacy path; the loaded/unloaded
        # competitor must stand aside while the FIFO owns the claim.
        assert fifo.queued()[0]['local_automation_v1']['kanban_cards']
        assert helpers._sweep([]) == []
        fifo.boundary()
        authority.sessions.clear()
        real_ack = notices.ack
        monkeypatch.setattr(notices, 'ack', lambda *_: False)
        tick(20)
        assert owner._admitted
        assert helpers._ledger(TID)['delivered'] == 0
        # OFF must not spawn another delivery owner or reset any durable cursor.
        monkeypatch.setenv('HERMES_FACTS_GATEWAY', '0')
        monkeypatch.setattr(notices, 'ack', real_ack)
        assert tick(30, legacy=True) == []
        assert not owner._admitted
        assert helpers._ledger(TID)['delivered'] == notices.DELIVERED
        assert helpers._sweep([]) == []
        assert len(helpers._rows(helpers._key(TID))) == 1
        assert fifo.queued() == []
        assert not scheduler.gateway_sources.actions
    finally:
        gate.close()
        scheduler.close()


def test_unloaded_incomplete_preserves_earlier_deadline():
    from tui_gateway.kanban_unloaded_gates import run, scheduling
    from tui_gateway.notice_fanout_result import FanoutResult
    result = FanoutResult()
    result.incomplete = True
    result.next_deadline = 3.0
    report = PassReport()
    with scheduling(report):
        run('card_change_pass', lambda: result, live=set())
    assert report.pending
    assert report.next_due(0, 60) == 3.0


def test_remote_incomplete_preserves_earlier_deadline(env, monkeypatch):
    from tui_gateway.notice_fanout_result import FanoutResult
    hub, runner, authority, fifo, db = env
    authority.sessions[helpers.CHAT] = helpers._Live(helpers.CHAT)
    helpers._busy(db, fifo)
    helpers._publish(hub, [])
    result = FanoutResult()
    result.incomplete = True
    result.next_deadline = 3.0
    monkeypatch.setattr(notices, 'collect', lambda *a, **kw: result)
    report = PassReport()
    owner._collect(runner, authority, owner._owned_lives(runner, authority), report)
    assert report.pending
    assert report.next_due(0, 10) == 3.0


@pytest.fixture(autouse=True)
def native_policy_opt_in(monkeypatch):
    from hermes_cli import config
    monkeypatch.setenv('HERMES_FACTS_GATEWAY', '1')
    monkeypatch.setattr(config, 'load_config_readonly',
                        lambda: {'display': {'gateway_notification_f': True}})
