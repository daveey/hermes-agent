"""Frozen pre-upgrade courier requests must retain their deployed wire identity."""
import copy
import hashlib

import pytest

from hermes_cli import kanban_sync_client as sync
from hermes_cli.fd_headroom_transport import Client


@pytest.mark.parametrize('prefix', ['fd-headroom:', 'fd-headroom-probe:'])
def test_persisted_legacy_request_reconciles_without_duplicate(monkeypatch, prefix):
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    legacy_id = prefix + 'worker-a1:default:gateway:2026-11-27T07:43:45.460000+00:00'
    wire_id = prefix + hashlib.sha256(legacy_id.encode()).hexdigest()
    envelope = {'id': legacy_id, 'args': {'idempotency_key': legacy_id}, 'action': 'create'}
    frozen = copy.deepcopy(envelope)
    calls = []
    held = {wire_id: {'state': 'done', 'result': {'task_id': 't_0123456789abcdef'}}}

    def request(identity, method, path, body, budget):
        calls.append((method, path))
        if method == 'POST':
            pytest.fail('existing pre-upgrade request must not be submitted again')
        rid = path.removeprefix('/v2/sync/requests/')
        if rid not in held:
            raise sync.HubError(404, '{"error":"no such request"}')
        return {'request': held[rid]}

    monkeypatch.setattr(sync, '_bounded_call', request)
    for _ in range(2):
        assert Client().create(envelope) == 't_0123456789abcdef'
        assert envelope == frozen
    assert calls == [('GET', '/v2/sync/requests/' + wire_id)] * 2


def test_nonstr_missing_body_preserves_original_error_without_post(monkeypatch):
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    error = sync.HubError(404, None)
    calls = []

    def request(identity, method, *args):
        calls.append(method)
        raise error

    monkeypatch.setattr(sync, '_bounded_call', request)
    with pytest.raises(sync.HubError) as raised:
        Client().create({'id': 'fd-headroom:incident'})
    assert raised.value is error
    assert calls == ['GET']
