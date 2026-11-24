"""Canonical forwarding retains sender scope, never the intake's ambient tenant."""
import json

import pytest

from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_worker_node as nodes
from tests.tools import test_kanban_worker_canonical_home as home
from tools import kanban_tools as tools
from tools.kanban_tools_create_args import effective_tenant

svc, gws, pool_labels = home.svc, home.gws, home.pool_labels


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('explicit', [None, 'tenant-explicit'])
def test_sender_tenant_survives_canonical_transport(gws, monkeypatch, explicit, legacy):
    if legacy:
        from hermes_cli import kanban_creation_home
        monkeypatch.setattr(kanban_creation_home, 'plan', lambda *a, **kw: None)
    _, worker = gws
    worker.enter()
    monkeypatch.setenv('HERMES_TENANT', 'tenant-source')
    monkeypatch.setattr(placement.reg, 'self_gateway', lambda: nodes.WORKER)
    monkeypatch.setattr(placement, 'config', lambda: {'worker_queue': {'hub': 'pool:worker'}})
    monkeypatch.setattr(placement, '_machine_picks_up', lambda labels: True)
    sent = []

    def send(view, **kwargs):
        sent.append(view)
        return {'ok': True, 'request_id': 'tenant-regression', 'queued_only': True}

    monkeypatch.setattr(placement, 'create_remote', send)
    args = {'title': 'tenant transport proof', 'assignee': 'default', 'requires': []}
    if explicit is not None:
        args['tenant'] = explicit
    expected = explicit or 'tenant-source'
    result = json.loads(tools._handle_create(args))
    if legacy:
        assert not sent
        row = worker.conn.execute('SELECT tenant FROM tasks WHERE id=?', (result['task_id'],)).fetchone()
        assert row[0] == expected
        return
    assert result.get('placed', {}).get('gateway') == 'hub', result
    assert effective_tenant(args, None) == expected
    monkeypatch.setenv('HERMES_TENANT', 'tenant-receiver')
    assert sent[0]['tenant'] == expected
    assert effective_tenant(sent[0], object()) == expected


@pytest.mark.parametrize('args', [{}, {'tenant': None}, {'tenant': 'tenant-explicit'}])
def test_courier_tenant_never_inherits_receiver_environment(monkeypatch, args):
    monkeypatch.setenv('HERMES_TENANT', 'tenant-receiver')
    assert effective_tenant(args, object()) == args.get('tenant')
