"""Public front doors route declared intake before board/placement effects."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_selfwork_outbound as outbound, kanban_selfwork_owner as owner
from tests.hermes_cli import test_kanban_selfwork_entry as entry
from tools import kanban_tools as kt

board = entry.board
local = entry.local


@pytest.fixture
def sender(local, monkeypatch, tmp_path):
    home = {'gateway': 'owner', 'profile': 'work', 'board': 'incidents'}
    monkeypatch.setattr(owner, '_runtime', lambda: ({'kanban': {'self_work': {'home': home}}},
                                                   'origin', 'default', tmp_path))
    monkeypatch.setattr('gateway.session_context.get_session_env', lambda name: '')
    return home


@pytest.mark.parametrize('surface', ['cli', 'tool'])
def test_real_entry_queues_without_local_preflight(sender, monkeypatch, surface):
    seen = []
    def run(argv, **kw):
        seen.append((argv, json.loads(kw['input'])))
        return SimpleNamespace(returncode=0, stdout=argv[argv.index('--id') + 1] + '\n', stderr='')
    monkeypatch.setattr(outbound.subprocess, 'run', run)
    monkeypatch.setattr(kt, '_board', lambda *a: pytest.fail('outbound opened local board'))
    monkeypatch.setattr('hermes_cli.kanban_hub_admission_placement.cli_preflight',
                        lambda *a, **kw: pytest.fail('outbound ran local placement'))
    result = entry.create(surface, entry.support.declaration())
    assert result == 0 if surface == 'cli' else result['code'] == 'self_work_queued', result
    assert len(seen) == 1
    argv, body = seen[0]
    assert argv[2:6] == ['owner', 'tasks', 'work/incidents', 'selfwork-create']
    assert body['create']['self_work'] == entry.support.declaration()
    assert body['create']['board'] == 'incidents'
    assert 'origin' not in body


def test_full_payload_and_destination_bound_retry(sender):
    args = {'title': 'evidence', 'self_work': entry.support.declaration(), 'runs_on': ['pool:worker'],
            'parents': ['t_a'], 'tenant': 'one', 'initial_status': 'todo'}
    home, body, rid = outbound.prepare(args)
    assert body['create']['runs_on'] == ['pool:worker']
    assert body['create']['parents'] == ['t_a']
    assert body['create']['tenant'] == 'one'
    assert outbound.prepare(args)[2] == rid
    sender['profile'] = 'other'
    assert outbound.prepare(args)[2] != rid
    assert home == sender


def test_explicit_same_gateway_origin_is_not_dropped(sender):
    _, body, _ = outbound.prepare({'title': 'x', 'self_work': entry.support.declaration(),
                                   'origin_session': '@session:work@origin/chat'})
    assert body['origin'] == {'gateway': 'origin', 'profile': 'work', 'session': 'chat'}


def test_foreign_origin_fails_closed(sender):
    with pytest.raises(ValueError, match='authenticated origin gateway'):
        outbound.prepare({'self_work': entry.support.declaration(), 'origin_session': 'foreign/work/chat'})


def test_worker_requires_authorized_forwarding(sender, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_WORKER_TASK', 't_worker')
    with pytest.raises(ValueError, match='authorized origin-gateway'):
        outbound.prepare({'self_work': entry.support.declaration()})


@pytest.mark.parametrize('code', [1, 2, 4])
def test_transport_failure_never_claims_created(sender, monkeypatch, code):
    monkeypatch.setattr(outbound.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=code, stdout='', stderr='unsupported'))
    with pytest.raises(ValueError, match='transport refused'):
        outbound.submit({'self_work': entry.support.declaration()})
