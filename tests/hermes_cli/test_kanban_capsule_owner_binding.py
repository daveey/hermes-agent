"""A capsule card's submitter refuses an owner that cannot hold the capsule gates. [daveey]

2026-11-02 hub, run4 of t_01a19a6b4fcce737: the dispatcher launched a pinned-core
``kanban_worker_client`` whose ordinary discovery reached the root multiplexer (another core, no
``gateway.session_kanban_capsule``). That owner admitted the capsule-required card and ran an ordinary
managed worker: real model/tool calls with no capsule and no observation. These tests drive the REAL
``kanban_worker_client.run`` and ``connect_gateway`` with only the discovery verdict and the ticket mint
faked, and prove the refusal happens before any ticket, socket or ``kanban.run``.
"""
import asyncio
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_client, kanban_worker_client
from hermes_cli.gateway_runtime import GatewayDiscovery, GatewayEndpoint
from hermes_cli.kanban_capsule_owner import CAPABILITY, REFUSED, CapsuleOwnerRefused, card_requires_capsule

BASE_CAPS = frozenset({'session-authority-v1', 'durable-admission-v1', 'event-replay-v1'})
OTHER_SHA = '16488722687057fcbf6861283e042feca062c5a0'
OWN_SHA = 'a' * 40


def _board(tmp_path, monkeypatch, *, local=False):
    from tests.gateway.test_session_kanban_capsule_guard import _board as guard_board
    _connection, params, _config = guard_board(tmp_path, monkeypatch, local=local)
    from hermes_cli.kanban_db import kanban_db_path
    return params, str(kanban_db_path(board='owned'))


def _endpoint(caps, sha):
    return GatewayEndpoint(profile_id='/p', instance_id='i', authority_epoch=1, runtime_protocol=1,
                           api_origin='http://127.0.0.1:1', supervisor='systemd', capabilities=frozenset(caps),
                           control_home='/root-home', code_sha=sha)


class _Stop(Exception):
    pass


def _discovery(monkeypatch, endpoint, calls):
    from hermes_cli import gateway_runtime, version_info

    def ensure(home, **kw):
        calls.append('ensure')
        return GatewayDiscovery('ready', endpoint)

    def ticket(*a, **kw):
        calls.append('ticket')
        raise _Stop('ticket minted: the owner check passed')

    monkeypatch.setattr(gateway_runtime, 'ensure_gateway_runtime', ensure)
    monkeypatch.setattr(gateway_client, '_session_ticket', ticket)
    monkeypatch.setattr(version_info, 'get_code_identity', lambda refresh=False: {'sha': OWN_SHA})
    monkeypatch.delenv('HERMES_TUI_GATEWAY_URL', raising=False)


def _run(params, db):
    return asyncio.run(kanban_worker_client.run(params, db))


@pytest.mark.parametrize('caps,sha', [
    (BASE_CAPS, OTHER_SHA),                 # the run4 owner: older core, no capsule gates
    (BASE_CAPS | {CAPABILITY}, OTHER_SHA),  # capsule-capable but not this submitter's core
    (BASE_CAPS | {CAPABILITY}, None),       # cannot name its code
    (BASE_CAPS, OWN_SHA),                   # same sha claimed, capability absent
])
def test_required_card_refuses_unbound_owner_before_ticket(tmp_path, monkeypatch, caps, sha):
    params, db = _board(tmp_path, monkeypatch)
    assert card_requires_capsule(db, params['task_id']) is True
    calls = []
    _discovery(monkeypatch, _endpoint(caps, sha), calls)
    with pytest.raises(CapsuleOwnerRefused) as exc:
        _run(params, db)
    assert REFUSED in str(exc.value)
    assert calls == ['ensure']  # final verdict: no retry, no ticket, no dial, no kanban.run


def test_required_card_reaches_bound_owner(tmp_path, monkeypatch):
    params, db = _board(tmp_path, monkeypatch)
    calls = []
    _discovery(monkeypatch, _endpoint(BASE_CAPS | {CAPABILITY}, OWN_SHA), calls)
    with pytest.raises(_Stop):
        _run(params, db)
    assert calls == ['ensure', 'ticket']


def test_ordinary_card_unchanged_with_any_owner(tmp_path, monkeypatch):
    params, db = _board(tmp_path, monkeypatch, local=True)
    assert card_requires_capsule(db, params['task_id']) is False
    calls = []
    _discovery(monkeypatch, _endpoint(BASE_CAPS, OTHER_SHA), calls)
    with pytest.raises(_Stop):
        _run(params, db)
    assert calls == ['ensure', 'ticket']


def test_unreadable_board_or_task_fails_closed(tmp_path, monkeypatch):
    params, db = _board(tmp_path, monkeypatch)
    assert card_requires_capsule(str(tmp_path / 'missing.db'), params['task_id']) is True
    assert card_requires_capsule(db, 't_missing') is True


def test_submitter_without_code_identity_refuses(tmp_path, monkeypatch):
    params, db = _board(tmp_path, monkeypatch)
    calls = []
    _discovery(monkeypatch, _endpoint(BASE_CAPS | {CAPABILITY}, OWN_SHA), calls)
    from hermes_cli import version_info
    monkeypatch.setattr(version_info, 'get_code_identity', lambda refresh=False: {'sha': None})
    with pytest.raises(CapsuleOwnerRefused, match='submitter code identity unknown'):
        _run(params, db)
    assert calls == ['ensure']


def test_explicit_endpoint_and_remote_url_paths_check(monkeypatch):
    seen = []

    def check(endpoint):
        seen.append(endpoint)
        raise CapsuleOwnerRefused('x')

    async def dial(**kw):
        async with gateway_client.connect_gateway(**kw):
            pass

    endpoint = _endpoint(BASE_CAPS, OTHER_SHA)
    with pytest.raises(CapsuleOwnerRefused):
        asyncio.run(dial(endpoint=endpoint, owner_check=check))
    assert seen == [endpoint]
    monkeypatch.setenv('HERMES_TUI_GATEWAY_URL', 'ws://127.0.0.1:9/x')
    with pytest.raises(gateway_client.GatewayClientError, match='cannot prove the owner binding'):
        asyncio.run(dial(owner_check=lambda e: None))


def test_submit_exit_is_a_counted_refusal_not_an_owner_wait(tmp_path, monkeypatch, capsys):
    params, db = _board(tmp_path, monkeypatch)
    calls = []
    _discovery(monkeypatch, _endpoint(BASE_CAPS, OTHER_SHA), calls)
    monkeypatch.setenv('HERMES_KANBAN_BOARD', params['board'])
    monkeypatch.setenv('HERMES_KANBAN_TASK', params['task_id'])
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(params['run_id']))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK', params['claim_lock'])
    monkeypatch.setenv('HERMES_KANBAN_DB', db)
    assert kanban_worker_client._submit() == 1
    assert REFUSED in capsys.readouterr().err
    assert calls == ['ensure']


def test_capsule_core_owner_advertises_capability(monkeypatch):
    from gateway import run_runtime, session_hosted_service
    monkeypatch.setattr(session_hosted_service, 'start_ready_hosted_services', lambda runner: None)
    runner = SimpleNamespace(session_runtime_descriptor={}, _running=True, _draining=False,
                             session_api=SimpleNamespace(task=SimpleNamespace(done=lambda: False)))
    run_runtime.publish_gateway_runtime_ready(runner)
    assert CAPABILITY in runner.session_runtime_descriptor['capabilities']
    assert {'session-authority-v1', 'durable-admission-v1', 'event-replay-v1'} <= set(
        runner.session_runtime_descriptor['capabilities'])
