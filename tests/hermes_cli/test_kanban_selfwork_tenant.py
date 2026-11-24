"""Independent review: sender ambient tenant survives courier serialization."""
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork_outbound as outbound
from hermes_cli import kanban_selfwork_intake as intake
from tests.hermes_cli import test_kanban_selfwork_outbound as support
from tools import kanban_tools as kt

board = support.board
local = support.local
sender = support.sender


@pytest.mark.parametrize('explicit', [False, True])
def test_public_tool_preserves_ambient_tenant(sender, local, monkeypatch, explicit):
    monkeypatch.setattr('gateway.session_context.get_session_env', lambda name, default='': default)
    captured = []
    def submit(args):
        _, body, request_id = outbound.prepare(args)
        captured.append(body['create'])
        return intake.admit(local, request_id=request_id, authority='home', local_gateway='home',
                            options=body['create'], now=1794690000)
    monkeypatch.setattr(outbound, 'submit', submit)
    receipts = []
    for tenant in ('tenant-a', 'tenant-b'):
        monkeypatch.setenv('HERMES_TENANT', tenant)
        # Ordinary product creation establishes the existing public-tool contract.
        product = json.loads(kt._handle_create({'title': 'Product ' + tenant, 'requires': [], 'placement': 'local',
                                               'session_id': '20261123_120000_selfwork'}))
        assert kb.get_task(local, product['task_id']).tenant == tenant
        receipts.append(json.loads(kt._handle_create({'title': 'Finding ' + tenant, 'requires': [],
                          **({'tenant': tenant} if explicit else {}),
                          'self_work': support.entry.support.declaration()})))
    actual = [kb.get_task(local, receipt['task_id']).tenant for receipt in receipts]
    assert actual == ['tenant-a', 'tenant-b'], {'actual_tenants': actual, 'payloads': captured,
                                               'same_owner': receipts[0]['task_id'] == receipts[1]['task_id']}


@pytest.mark.parametrize('surface', ['cli', 'tool'])
@pytest.mark.parametrize('tenant', [None, 'sender-tenant'])
def test_real_front_door_freezes_sender_scope(sender, monkeypatch, surface, tenant):
    from types import SimpleNamespace

    if tenant is None:
        monkeypatch.delenv('HERMES_TENANT', raising=False)
    else:
        monkeypatch.setenv('HERMES_TENANT', tenant)
    captured = []
    def run(argv, **kwargs):
        captured.append(json.loads(kwargs['input'])['create'])
        return SimpleNamespace(returncode=0, stdout=argv[argv.index('--id') + 1] + '\n', stderr='')
    monkeypatch.setattr(outbound.subprocess, 'run', run)
    result = support.entry.create(surface, support.entry.support.declaration())
    assert result == 0 if surface == 'cli' else result['code'] == 'self_work_queued'
    assert len(captured) == 1
    assert 'tenant' in captured[0]
    assert captured[0]['tenant'] == tenant


def test_scope_is_in_request_identity_and_explicit_scope_wins(sender, monkeypatch):
    args = {'title': 'Identical finding', 'self_work': support.entry.support.declaration()}
    monkeypatch.setenv('HERMES_TENANT', 'tenant-a')
    _, first, first_id = outbound.prepare(args)
    assert outbound.prepare(args)[2] == first_id
    monkeypatch.setenv('HERMES_TENANT', 'tenant-b')
    _, second, second_id = outbound.prepare(args)
    assert first_id != second_id
    assert first['create']['tenant'] == 'tenant-a'
    assert second['create']['tenant'] == 'tenant-b'
    assert 'tenant' not in args
    _, explicit, explicit_id = outbound.prepare({**args, 'tenant': 'tenant-a'})
    assert explicit['create']['tenant'] == 'tenant-a'
    assert explicit_id == first_id

