"""Explicit chat custody must not bypass canonical pool-worker creation."""
import json

import pytest

from hermes_cli import kanban_origin as origin
from hermes_cli import kanban_placement as placement
from tools import kanban_tools as tools
from tests.tools import test_kanban_worker_canonical_home as home

svc = home.svc
gws = home.gws
pool_labels = home.pool_labels


@pytest.mark.parametrize('ambient', [False, True])
@pytest.mark.parametrize('options,labels', [
    ({'runs_on': ['pool:worker']}, ['pool:worker']),
    ({'runs_on': []}, []),
    ({'skills': ['session-status-panel', 'inspecting-hermes-desktop-dom']},
     ['pool:worker', 'capability:tailnet-exec']),
    ({'placement': 'local'}, ['machine:worker-1']),
])
def test_explicit_origin_forwards_worker_home(gws, monkeypatch, ambient, options, labels):
    _, worker = gws
    worker.enter()
    ref = origin.Origin('hub', 'default', 'canonical-home-owner')
    # Origin resolution/authentication has its own real-courier suites; here retain
    # the resolved identity through the actual tool's placement branch.
    monkeypatch.setattr(origin, 'resolve', lambda value: ref)
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: home.nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda _: True)
    # Model the pre-existing admitted creator evidence only; receiving origin
    # authorization is deliberately not replaced by this sender-branch test.
    monkeypatch.setattr(tools, '_forwarding_fields', lambda *a: {
        'origin_gateway': ref.gateway, 'creator': {'task_id': 'admitted-creator'}, 'run_uid': 'admitted-run'})
    captured = []

    def courier(view, **context):
        captured.append((view, context))
        return {'ok': True, 'request_id': 'explicit-origin-home', 'queued_only': True}

    monkeypatch.setattr(placement, 'create_remote', courier)
    before = worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    args = {'title': 'Origin-bound portable review', 'body': 'Inspect the Desktop DOM',
            'assignee': 'default', 'requires': [], **options}
    if ambient:
        monkeypatch.setenv(origin.ENV, ref.ref)
    else:
        args['origin_session'] = ref.ref
    out = json.loads(tools._handle_create(args))
    assert len(captured) == 1, out
    view, context = captured[0]
    assert context['target'] == 'hub'
    assert context['origin_session'] == ref.session
    assert context['origin_profile'] == ref.profile
    assert context['origin_gateway'] == ref.gateway
    assert context['canonical_home'] is True
    assert view['runs_on'] == labels
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before


@pytest.mark.parametrize('failure', ['missing_creator', 'receiver_refusal', 'planner_exception'])
def test_explicit_origin_never_falls_back_to_worker(gws, monkeypatch, failure):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(origin, 'resolve', lambda value: origin.Origin('hub', 'default', 'canonical-home-owner'))
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: home.nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    if failure == 'receiver_refusal':
        monkeypatch.setattr(tools, '_forwarding_fields', lambda *a: {'origin_gateway': 'hub'})
        monkeypatch.setattr(placement, 'create_remote', lambda *a, **kw: {'ok': False, 'error': 'origin denied'})
    elif failure == 'planner_exception':
        from hermes_cli import kanban_creation_home

        def unavailable(*a, **kw):
            raise RuntimeError('registry unavailable')

        monkeypatch.setattr(kanban_creation_home, 'plan', unavailable)
    before = worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    out = json.loads(tools._handle_create({'title': 'Origin refusal', 'requires': [], 'runs_on': ['pool:worker']}))
    assert not out.get('task_id'), out
    assert out.get('error'), out
    assert worker.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before


def test_explicit_origin_nonpool_keeps_existing_storage(gws, monkeypatch):
    _, worker = gws
    worker.enter()
    monkeypatch.setattr(origin, 'resolve', lambda value: origin.Origin('hub', 'default', 'canonical-home-owner'))
    monkeypatch.setattr(placement, 'config', dict)
    monkeypatch.setattr(placement, 'create_remote', lambda *a, **kw: pytest.fail('nonpool forwarded'))
    out = json.loads(tools._handle_create({'title': 'Nonpool origin card', 'requires': []}))
    assert out.get('task_id'), out
    assert out['placement']['where'] == 'local'
