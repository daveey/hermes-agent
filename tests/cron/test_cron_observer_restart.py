"""A scoped firer observes a canonical owner; losing its socket is not losing the fire."""
from contextlib import asynccontextmanager
import json

import pytest

from cron import scheduler_authority as authority
from hermes_cli import gateway_client


@pytest.mark.parametrize('lost_at', ['submit', 'status'])
@pytest.mark.parametrize('success', [True, False])
def test_observer_recovers_same_fire_after_restart(monkeypatch, lost_at, success):
    calls = []
    connections = []
    result = [success, 'durable document', 'answer' if success else '', None if success else 'interrupted']

    class Peer:
        def __init__(self, number):
            self.number = number

        async def rpc(self, method, **params):
            calls.append((self.number, method, params))
            if self.number == 0 and method == 'cron.' + lost_at:
                raise gateway_client.GatewayClientError(gateway_client.DISCONNECTED)
            if method == 'cron.submit':
                return {'session_id': 'session', 'admission_id': 'admission'}
            if method == 'cron.recover':
                assert params == {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None}
                return {'status': 'terminal', 'session_id': 'session', 'admission_id': 'admission'}
            assert params == {'session_id': 'session', 'admission_id': 'admission'}
            return {'status': 'terminal', 'result': result}

    @asynccontextmanager
    async def connect():
        peer = Peer(len(connections))
        connections.append(peer)
        yield peer

    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    assert authority.run_canonical_job({'id': 'job'}, execution_id='fire') == tuple(result)
    assert sum(method == 'cron.submit' for _, method, _ in calls) == 1
    assert len(connections) == 2
    record = json.loads(authority.journal_path('job', 'fire').read_text())
    assert record['receipt'] == {'session_id': 'session', 'admission_id': 'admission'}


def test_existing_journal_is_recovered_not_resubmitted(monkeypatch):
    path = authority.journal_path('job', 'fire')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'params': {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None},
                                'receipt': None}))
    calls = []

    class Peer:
        async def rpc(self, method, **params):
            calls.append(method)
            assert method == 'cron.recover', 'an existing fire must not be submitted again'
            return {'status': 'missing', 'session_id': 'session', 'admission_id': 'admission'}

    @asynccontextmanager
    async def connect():
        yield Peer()

    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    with pytest.raises(authority.CronExecutionUnknown):
        authority.run_canonical_job({'id': 'job'}, execution_id='fire')
    assert calls == ['cron.recover']


def test_recovery_refusal_is_not_treated_as_a_submit_refusal(monkeypatch):
    monkeypatch.setattr('cron.observer_reconnect.RECONNECT_SECONDS', 0)
    path = authority.journal_path('job', 'fire')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'params': {'job_id': 'job', 'request_id': 'fire', 'extra_prompt': None},
                                'receipt': None}))
    calls = []

    class Peer:
        async def rpc(self, method, **params):
            calls.append(method)
            raise gateway_client.GatewayRPCError('permission_denied')

    @asynccontextmanager
    async def connect():
        yield Peer()

    monkeypatch.setattr(gateway_client, 'connect_gateway', connect)
    with pytest.raises(authority.CronExecutionUnknown):
        authority.run_canonical_job({'id': 'job'}, execution_id='fire')
    assert calls == ['cron.recover']
