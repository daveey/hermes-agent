"""Courier's deployed route/body grammar, exercised with real incident envelopes."""
import re

import pytest

from hermes_cli import fd_headroom_incident as incident
from hermes_cli import kanban_sync_client as sync
from hermes_cli.fd_headroom_transport import Client


@pytest.mark.parametrize('host', ['hermes-hub', 'worker-' + 'a' * 180])
def test_incident_create_and_comment_use_replayable_wire_ids(monkeypatch, host):
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    requests = {}
    paths = []
    # index.ts's GET route and hub.ts's OPID_RE do not URL-decode IDs.
    route = re.compile(r'/v2/sync/requests/([A-Za-z0-9._:-]{1,128})\Z')
    opid = re.compile(r'[A-Za-z0-9._:-]{8,128}\Z')
    def request(ident, method, path, body, budget_s):
        paths.append(path)
        if method == 'GET':
            match = route.fullmatch(path)
            assert match, f'GET does not match deployed request route: {path}'
            if match[1] not in requests:
                raise sync.HubError(404, '{"error":"no such request"}')
            return {'request': requests[match[1]]}
        assert opid.fullmatch(body['id']), f'POST ID rejected by deployed OPID_RE: {body["id"]}'
        existing = requests.get(body['id'])
        if existing:
            assert existing['envelope'] == body
        requests[body['id']] = {'state': 'done', 'result': {'task_id': 't_0123456789abcdef'}, 'envelope': body}
        return {}
    monkeypatch.setattr(sync, '_bounded_call', request)
    state = {}
    sample = dict(host=host, profile='default', role='backend', count=100, soft=128,
                  census={'classes': {'other': 100}}, triggered=True)
    incident._new_envelope(sample, state, 1795773117.972333, True, 'fd-headroom-proof')
    envelope = state['envelope']
    original_id = envelope['id']
    original_key = envelope['args']['idempotency_key']
    assert '+00:00' in original_id
    assert Client().create(envelope) is None
    assert Client().create(envelope) == 't_0123456789abcdef'
    assert len(requests) == 1
    assert envelope['id'] == original_id
    assert next(iter(requests.values()))['envelope']['args']['idempotency_key'] == original_key
    comment = dict(id=original_id + ':comment:1795773200', to_gw='hub', coll='tasks',
                   key='default/fd-headroom-proof/t_0123456789abcdef', action='comment', args={'text': 'headroom'})
    Client().comment(comment)
    Client().comment(comment)
    assert len(requests) == 2
    assert all('%' not in path for path in paths)


def test_valid_existing_request_identity_is_not_rekeyed(monkeypatch):
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    paths = []
    def request(ident, method, path, body, budget_s):
        paths.append(path)
        return {'request': {'state': 'done', 'result': {'task_id': 't_0123456789abcdef'}}}
    monkeypatch.setattr(sync, '_bounded_call', request)
    assert Client().create({'id': 'fd-headroom:legacy-valid'}) == 't_0123456789abcdef'
    assert paths == ['/v2/sync/requests/fd-headroom:legacy-valid']
