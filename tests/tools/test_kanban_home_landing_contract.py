"""Canonical-home parity must reach stored origin rows, not stop at the sender wire."""
import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_hub_admission as judge
from tests.tools import test_kanban_worker_canonical_home as home
from tools import kanban_tools as tools

svc, gws, pool_labels = home.svc, home.gws, home.pool_labels
REQUEST = {'action': 'Reply A or B in this chat. Unblock = implement A; archive = drop it.',
           'why_human': 'Only the owner can choose this outcome.', 'interaction': 'reply',
           'scope': 'Fixture decision', 'evidence': 'Two equally supported alternatives.'}


@pytest.mark.parametrize('requires', [[], ['terminal']])
@pytest.mark.parametrize('labels', [[], ['pool:worker'], ['machine:macbook']])
def test_origin_landing_stores_exact_execution_contract(gws, monkeypatch, labels, requires):
    hub, _worker = gws
    hub.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: 'hub')
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)

    def no_judge(*a, **kw):
        pytest.fail('An any-machine or non-hub lease is not a hub execution pin')

    monkeypatch.setattr(judge, 'admit', no_judge)
    intent = {'request_id': 'contract-origin-landing',
              'owner': {'gateway': 'hub', 'profile': 'default', 'board': 'default'},
              'origin': {'gateway': 'worker-1', 'profile': 'default', 'session': 'canonical-home-owner'}}
    args = {'title': 'Origin execution contract', 'assignee': 'default',
            'requires': requires, 'runs_on': labels}
    first = json.loads(tools._handle_create(dict(args), origin_intent=intent))
    assert first.get('task_id'), first
    row = hub.conn.execute('SELECT runs_on,requires FROM tasks WHERE id=?', (first['task_id'],)).fetchone()
    assert json.loads(row['runs_on']) == labels
    assert json.loads(row['requires']) == requires
    second = json.loads(tools._handle_create(dict(args), origin_intent=intent))
    assert second.get('task_id') == first['task_id'], second
    assert hub.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 1


@pytest.mark.parametrize('options', [
    {'max_retries': 2}, {'max_retries': 10},
    {'initial_status': 'blocked', 'human_request': REQUEST},
])
def test_supported_hold_and_retry_options_reach_hub_row(gws, monkeypatch, options):
    hub, worker = gws
    worker.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway',
                        lambda: 'hub' if str(hub.home) == os.environ['HERMES_HOME'] else 'worker-1')
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    received = []

    def courier(view, **kwargs):
        assert kwargs['target'] == 'hub'
        from hermes_cli import kanban_creation_home
        carried = kanban_creation_home.transport_options(view, hub_cli='fixture-courier')
        assert all(carried[k] == v for k, v in options.items())
        intent = {'request_id': 'contract-hold-landing',
                  'owner': {'gateway': 'hub', 'profile': 'default', 'board': 'default'},
                  'origin': {'gateway': 'worker-1', 'profile': 'default', 'session': 'canonical-home-owner'}}
        hub.enter()
        try:
            result = json.loads(tools._handle_create(view, origin_intent=intent))
            assert result.get('task_id'), result
            received.append(result['task_id'])
        finally:
            worker.enter()
        return {'ok': True, 'request_id': 'contract-hold-landing', 'queued_only': True}

    monkeypatch.setattr(placement, 'hub_runner', lambda argv: (0, '--option canonical-home-v1', ''))
    monkeypatch.setattr(placement, 'create_remote', courier)
    result = json.loads(tools._handle_create({'title': 'Retry and hold contract', 'assignee': 'default',
                                             'requires': [], **options}))
    assert result.get('placed', {}).get('gateway') == 'hub', result
    assert len(received) == 1
    row = kb.get_task(hub.conn, received[0])
    assert row is not None
    if 'max_retries' in options:
        assert row.max_retries == options['max_retries']
    else:
        assert row.status == 'blocked'
        assert row.human_request == REQUEST
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0


def test_any_machine_landing_under_write_lock_keeps_empty_contract(gws, monkeypatch):
    from hermes_cli.kanban_hub_admission_placement import courier_plan
    hub, _worker = gws
    hub.enter()
    with kb.write_txn(hub.conn):
        assert hub.conn.in_transaction
        assert courier_plan(hub.conn, {'placement': 'auto', 'requires': []}, [],
                            cfg={'worker_queue': {'hub': 'pool:worker'}}, gateway='hub') is None


@pytest.mark.parametrize('labels,pin', [(['machine:hub'], 'auto'), ([], 'hub')])
def test_genuine_hub_execution_still_needs_admission(gws, monkeypatch, labels, pin):
    from hermes_cli.kanban_hub_admission_placement import courier_plan
    hub, _worker = gws
    hub.enter()
    monkeypatch.setattr(judge, 'admit', lambda *a, **kw: judge.Verdict())
    result = courier_plan(hub.conn, {'placement': pin, 'requires': []}, labels,
                          cfg={'worker_queue': {'hub': 'pool:worker'}}, gateway='hub')
    assert result['where'] == 'refuse'
    assert result['hub_admission']['admit'] is False


def test_old_empty_lease_envelope_replays_original_denied_route(gws, monkeypatch):
    from hermes_cli import kanban_hub_admission_placement as admission
    hub, _worker = gws
    hub.enter()
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: 'hub')
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    machines = {k: dict(v) for k, v in home.nodes.MACHINES.items()}
    machines['worker-1']['labels'] = ['linux', 'pool:worker', 'capability:hub-write']
    monkeypatch.setattr(home.nodes, 'MACHINES', machines)
    intent = {'request_id': 'legacy-empty-origin',
              'owner': {'gateway': 'hub', 'profile': 'default', 'board': 'default'},
              'origin': {'gateway': 'worker-1', 'profile': 'default', 'session': 'canonical-home-owner'}}
    args = {'title': 'Previously applied empty lease', 'assignee': 'default', 'requires': [], 'runs_on': []}
    # Seed the actual prior-version envelope via common create, not a raw intent-row edit.
    with monkeypatch.context() as old:
        old.setattr(admission, 'courier_plan', lambda *a, **kw: {
            'where': 'local', 'why': 'legacy denied route', 'requires': ['hub:write'],
            'runs_on': ['pool:worker', 'capability:hub-write'],
            'hub_admission': {'admit': False, 'why': 'admission unavailable', 'entry': 'courier'}})
        first = json.loads(tools._handle_create(dict(args), origin_intent=intent))
    assert first.get('task_id'), first
    second = json.loads(tools._handle_create(dict(args), origin_intent=intent))
    assert second.get('task_id') == first['task_id'], second
    row = hub.conn.execute('SELECT runs_on,requires FROM tasks WHERE id=?', (first['task_id'],)).fetchone()
    assert json.loads(row['runs_on']) == ['capability:hub-write', 'pool:worker']
    assert json.loads(row['requires']) == ['hub:write']
