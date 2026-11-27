"""Real client entrypoint, isolated HTTP boundary; no courier subprocesses."""
import pytest
from hermes_cli import kanban_sync_client as sync


def test_transport_freezes_request_closes_and_bounds(monkeypatch):
    from hermes_cli.fd_headroom_transport import Client
    calls = []
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    def request(ident, method, path, body, budget_s):
        calls.append((method, path, body, budget_s))
        if method == 'GET' and '/requests/' in path:
            raise sync.HubError(404, '{"error":"no such request"}')
        return {}
    monkeypatch.setattr(sync, '_bounded_call', request)
    client = Client()
    envelope = dict(id='fd-headroom:incident', to_gw='hub', coll='tasks', action='create',
                    key='default/default/new', args={'title': 'headroom'})
    assert client.create(envelope) is None
    assert calls[-1][:3] == ('POST', '/v2/sync/requests', envelope)
    assert all(0 < call[3] <= 20 for call in calls)
    client.close()


def test_request_failure_not_absence_and_pending_reconciles(monkeypatch):
    from hermes_cli.fd_headroom_transport import Client
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    response = {'request': {'state': 'done', 'result': {'task_id': 't_0123456789abcdef'}}}
    monkeypatch.setattr(sync, '_bounded_call', lambda *a: response)
    assert Client().create({'id': 'same-key'}) == 't_0123456789abcdef'
    def denied(*a):
        raise sync.HubError(403, 'private auth failure')
    monkeypatch.setattr(sync, '_bounded_call', denied)
    with pytest.raises(sync.HubError):
        Client().read('t_0123456789abcdef', 'default')


def test_authoritative_evidence_requires_successful_missing_or_live(monkeypatch):
    from hermes_cli.fd_headroom_transport import Client
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    result = {'state': 'absent', 'table': 'tasks', 'uid': 't_0123456789abcdef', 'head': 4}
    monkeypatch.setattr(sync, '_bounded_call', lambda *a: result)
    assert Client().read(result['uid'], 'default') is None
    result['state'] = 'unknown'
    with pytest.raises(ValueError):
        Client().read(result['uid'], 'default')
