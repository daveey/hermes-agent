"""Actual alarm/courier producers cross common ingress with legacy evidence."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_capability_routing as kcr
from tests.hermes_cli import test_kanban_capability_routing as cap
from tests.hermes_cli import test_kanban_pickup_stall as pickup
from tests.hermes_cli import test_kanban_pickup_stall_held as held
from tests.hermes_cli.test_kanban_history_contract import decoded, LEGACY

svc = pickup.svc
gws = pickup.gws
alarm_worker_pool = pickup.alarm_worker_pool
pair = held.pair
env = cap.env


def test_pickup_legacy_source(gws, monkeypatch):
    hub, worker = gws
    pickup._no_profiles(monkeypatch)
    tid = pickup._pinned(hub)
    hub.conn.execute('UPDATE tasks SET title=? WHERE id=?', (LEGACY, tid))
    hub.round()
    worker.round()
    worker.tick()
    pickup._age(worker, tid, 601)
    worker.tick()
    (row,) = pickup._repairs(worker.conn)
    assert LEGACY in decoded(row['body'])
    assert row['model_override'] is None and row['provider_override'] is None
    assert kb.get_task(hub.conn, tid).title == LEGACY
    worker.tick()
    assert len(pickup._repairs(worker.conn)) == 1


def test_held_creator_legacy_source(pair):
    owner, _free, t0, _registry = pair
    tid = held._own_card(owner)
    owner.conn.execute('UPDATE tasks SET title=? WHERE id=?', (LEGACY, tid))
    owner.round()
    owner.enter()
    from hermes_cli import kanban_pickup_stall_held as detector
    cards = [{'id': tid, 'title': LEGACY, 'tenant': None, 'since_ms': int((t0 - 900) * 1000)}]
    entry = held._machines(t0)[held.HELD]
    child = kb.get_task(owner.conn, detector.file_alarm(owner.conn, held.HELD, cards,
                                                      now_ms=int(t0 * 1000), stall_s=600, entry=entry))
    assert LEGACY in decoded(child.body)
    assert 'do not lift or shorten the hold' in child.body
    assert kb.get_task(owner.conn, tid).title == LEGACY


def test_remote_capability_history_crosses_common_create(env):
    cap.publish(env['hub'], 'peer', {'ssh:fixture': time.time()})
    with kbc.connect_closing() as conn:
        tid = cap.running(conn)
        conn.execute('UPDATE tasks SET title=? WHERE id=?', (LEGACY, tid))
        kcr.route(conn, tid, ['ssh:fixture'], reason='Read service logs')
        (argv,) = env['calls']
        title, body = argv[3], argv[argv.index('--body') + 1]
        child = kb.get_task(conn, kb.create_task(conn, title=title, body=body))
        assert LEGACY in decoded(child.body)
        assert 'Read service logs' in child.body
        assert kb.get_task(conn, tid).title == LEGACY
        assert child.model_override is None and child.provider_override is None


def test_duplicate_detector_history_crosses_common_create(env):
    from hermes_cli import kanban_dup_detector as detector
    with kbc.connect_closing() as conn:
        body = detector._body('Past handoff: ' + LEGACY)
        assert LEGACY in decoded(body)
        assert kb.create_task(conn, title=detector.TITLE, body=body)
        with pytest.raises(ValueError, match='model-neutral'):
            kb.create_task(conn, title=detector.TITLE, body=body + '\nUse only grok-4.6')
