"""Board route/ID regressions from read-only deployed-service observations."""
import re

import pytest
from hermes_cli import kanban_sync_client as sync
from hermes_cli import resource_limits as limits
from tests.hermes_cli.test_fd_headroom import guard as guard  # noqa: PLC0414 -- pytest fixture reexport


@pytest.mark.parametrize('rid', ['fd-headroom:incident',
    'fd-headroom:worker:default:gateway:2026-11-27T07:43:45.460000+00:00',
    'fd-headroom:' + 'x' * 150], ids=['colon', 'legacy-iso', 'long'])
def test_real_route_missing_then_same_envelope_reconciles(monkeypatch, rid):
    from hermes_cli.fd_headroom_transport import Client
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    held, posts, paths = {}, [], []
    def call(ident, method, path, body, budget):
        if method == 'GET':
            paths.append(path)
            match = re.fullmatch(r'/v2/sync/requests/([A-Za-z0-9._:-]{1,128})', path)
            if not match:
                raise sync.HubError(404, '{"error":"not found"}')
            if match[1] not in held:
                raise sync.HubError(404, '{"error":"no such request"}')
            return {'request': held[match[1]]}
        assert re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', body['id'])
        posts.append(body)
        held[body['id']] = {'state': 'queued', 'result': None}
        return {}
    monkeypatch.setattr(sync, '_bounded_call', call)
    envelope = {'id': rid, 'args': {'idempotency_key': rid}, 'action': 'create'}
    original = dict(envelope)
    assert Client().create(envelope) is None
    assert Client().create(envelope) is None
    assert len(posts) == 1 and paths[0] == paths[1]
    assert posts[0]['args'] == envelope['args'] and envelope == original
    held[posts[0]['id']] = {'state': 'done', 'result': {'task_id': 't_0123456789abcdef'}}
    assert Client().create(envelope) == 't_0123456789abcdef'
    assert len(posts) == 1


@pytest.mark.parametrize(('status', 'detail'), [(404, '{"error":"not found"}'),
    (404, 'bad JSON'), (403, '{"error":"no such request"}'), (503, 'unavailable')])
def test_only_typed_missing_allows_post(monkeypatch, status, detail):
    from hermes_cli.fd_headroom_transport import Client
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    calls = []
    def fail(ident, method, *args):
        calls.append(method)
        raise sync.HubError(status, detail)
    monkeypatch.setattr(sync, '_bounded_call', fail)
    with pytest.raises(sync.HubError):
        Client().create({'id': 'fd-headroom:incident'})
    assert calls == ['GET']


def test_legacy_comment_id_is_accepted_by_board(monkeypatch):
    from hermes_cli.fd_headroom_transport import Client
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    calls = []
    monkeypatch.setattr(sync, '_bounded_call', lambda *args: calls.append(args))
    envelope = {'id': 'fd-headroom:legacy+00:00:comment:12345', 'args': {'text': 'sample'}}
    Client().comment(envelope)
    assert re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', calls[0][3]['id'])
    assert calls[0][3]['args'] == envelope['args']


@pytest.mark.parametrize('state', ['queued', 'claimed', 'waiting', 'rejected', 'failed', 'expired'])
def test_held_request_never_resends(monkeypatch, state):
    from hermes_cli.fd_headroom_transport import Client
    monkeypatch.setattr(sync, 'load_identity', lambda: sync.Identity('http://example.invalid', 'unused', 'worker'))
    calls = []
    def held(ident, method, *args):
        calls.append(method)
        return {'request': {'state': state, 'result': None}}
    monkeypatch.setattr(sync, '_bounded_call', held)
    if state in {'rejected', 'failed', 'expired'}:
        with pytest.raises(RuntimeError):
            Client().create({'id': 'fd-headroom:incident'})
    else:
        assert Client().create({'id': 'fd-headroom:incident'}) is None
    assert calls == ['GET']


@pytest.mark.parametrize('final_count', [367, 2047, 2048])
def test_startup_slope_requires_half_soft_before_warning(guard, final_count, caplog):
    resource, snapshot, notify = guard
    resource.getrlimit.return_value = (4096, 10000)
    for now, count in [(0, 15), (300, 100), (600, 200), (900, 300), (1200, final_count)]:
        snapshot.return_value = (count, {})
        sample = limits.check_fd_headroom({}, now=now)
    assert sample['slope'] > 0
    assert sample['triggered'] == (final_count >= 2048)
    assert notify.call_count == int(final_count >= 2048)
    if final_count < 2048:
        assert 'fd-headroom:' not in caplog.text


def test_prediction_still_warns_before_configured_high_ratio(guard):
    _, snapshot, notify = guard
    for now, count in [(0, 100), (400, 200), (800, 400), (1200, 500)]:
        snapshot.return_value = (count, {})
        sample = limits.check_fd_headroom({'runtime': {'fd_headroom': {'warn_ratio': .7}}}, now=now)
    assert sample['triggered'] and notify.call_count == 1


@pytest.mark.parametrize('first', ['gateway', 'backend'])
def test_one_launch_role_for_embedded_service_ticks(monkeypatch, first):
    monkeypatch.setattr(limits, '_fd_role', None, raising=False)
    monkeypatch.setattr(limits, '_fd_thread', None)
    calls = []
    monkeypatch.setattr(limits, 'gateway_fd_headroom_tick', lambda: calls.append('gateway'))
    monkeypatch.setattr(limits, 'check_fd_headroom', lambda **kw: calls.append(kw['role']))
    class InlineThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
        def is_alive(self):
            return False
    monkeypatch.setattr(limits.threading, 'Thread', InlineThread)
    limits.request_fd_headroom(role=first)
    limits.request_fd_headroom(role='backend' if first == 'gateway' else 'gateway')
    assert calls == [first, first]
    # Forked children may launch a different service; do not inherit the parent's role.
    monkeypatch.setattr(limits.os, 'getpid', lambda: 987654321)
    limits.request_fd_headroom(role='backend')
    assert calls[-1] == 'backend'
