"""Review probe: existing chat-bound self-work crosses new owner validation."""
import pytest
from hermes_cli import kanban_selfwork_outbound as outbound
from hermes_cli import kanban_selfwork_owner as owner
from hermes_cli import kanban_creation_contract as contract
from tests.hermes_cli import test_kanban_selfwork_nochat as support
world = support.world
request_envelope = support.request_envelope

@pytest.mark.parametrize('diagnostic', [False, True])
@pytest.mark.parametrize('case', ['control', 'null_tenant', 'scratch', 'placement'])
def test_existing_selfwork_sender_receiver(request_envelope, monkeypatch, capsys, tmp_path, diagnostic, case):
    home = {'gateway': 'owner', 'profile': 'default', 'board': 'default'}
    args = dict(request_envelope['args']['create'])
    args['session_id'] = 'creator'
    if case == 'null_tenant':
        monkeypatch.delenv('HERMES_TENANT', raising=False)
    else:
        monkeypatch.setenv('HERMES_TENANT', 'tenant-source')
    if case == 'scratch':
        args['workspace_kind'] = 'scratch'
    if case == 'placement':
        args['placement'] = 'auto'
    with monkeypatch.context() as sender:
        sender.setattr(owner, '_runtime', lambda: ({'kanban': {'self_work': {'home': home}}}, 'origin', 'default', tmp_path))
        sender.setattr('hermes_cli.kanban_origin.ambient_origin_ref', lambda: None)
        sender.delenv('HERMES_KANBAN_TASK', raising=False)
        sender.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
        _, body, rid = outbound.prepare(args)
    request = {**request_envelope, 'args': body, 'id': rid}
    if diagnostic:
        monkeypatch.setattr(contract, 'validate_forward', lambda args: None)
    result = support.invoke(request, monkeypatch, capsys)
    assert result.get('created') is True, {'case': case, 'create': body['create'], 'result': result}
