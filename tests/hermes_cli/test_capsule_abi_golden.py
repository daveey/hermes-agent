"""Frozen v2 behavioral ABI: changes require a deliberate contract migration.

These are wire/semantic vectors, not a substitute for native capsule execution.
"""
import json
import os
import socket

import pytest

from agent import observation_bind, observation_load
from gateway import session_kanban_capsule as capsule
from gateway import session_kanban_rendezvous as rendezvous


def test_observer_digest_golden_and_order_independence():
    files = {'observer_codex.py': b'codex\n', 'observer_runtime.py': b'runtime\x00\n',
             'observer_wire.py': b'wire\n'}
    expected = '3a4d28dc3a57da515a2a6b8be9bf30d062a00a30f8d3f5e1e01b3c1eedb686c9'
    assert observation_load.observer_digest(files) == expected
    assert observation_load.observer_digest(dict(reversed(list(files.items())))) == expected
    files['observer_wire.py'] += b' '
    assert observation_load.observer_digest(files) != expected


def test_frozen_observer_sentinel_interface():
    assert observation_load.OBSERVER_FILES == ('observer_codex.py', 'observer_runtime.py', 'observer_wire.py')
    assert observation_load.LOAD_ORDER == ('observer_wire', 'observer_runtime', 'observer_codex')
    assert observation_load.SENTINELS == (
        ('agent.chat_completion_helpers', '@_observe_sdk_request'),
        ('agent.codex_runtime', 'return open_observed_codex_stream(agent, active_client,'),
        ('agent.chat_completion_helpers', '@_unsupported_observed_summary'),
        ('agent.client_lifecycle', 'and not observation_active()'),
        ('agent.tool_executor', '@_observe_terminal_commit'),
        ('agent.turn_api_call', 'if observation_active():'),
        ('agent.session_persistence', 'observe_persisted(self, result)'),
        ('agent.tool_executor', 'observe_terminal(agent, function_name'),
        ('agent.managed_worker', 'observation_finish('),
    )
    observation_load.check_hooks()


def test_rendezvous_claim_and_observer_wire_golden(tmp_path, monkeypatch):
    db = tmp_path / 'board.db'
    db.touch()
    context = {'db': str(db), 'task_id': 't_golden', 'run_id': 7, 'claim_lock': 'claim-golden'}
    assert rendezvous.identity(context) == context
    assert rendezvous.IDENTITY == ('db', 'task_id', 'run_id', 'claim_lock')
    assert rendezvous.REGISTRATION_KEYS == {'v', 'db', 'dispatch_owner', 'pid', 'start',
                                            'observer_root', 'observer_digest'}
    assert rendezvous.REPLY_KEYS == {'v', 'ok', 'db', 'task_id', 'run_id', 'claim_lock', 'dispatch_owner'}
    assert observation_load.LINE_KEYS == {'v', 'op', 'root', 'digest', 'host_pid'}
    left, right = socket.socketpair()
    pin = {'observer_root': '/reviewed/observer', 'observer_digest': 'a' * 64, 'pid': 123}
    transport = rendezvous.Rendezvous(context, left, pin)
    child = None
    try:
        child, argv, kwargs = transport.prepare(['owner-python'], {})
        assert argv == ['owner-python', '--observation-fd', str(child.fileno())]
        assert kwargs == {'pass_fds': (child.fileno(),)}
        expected = (b'{"digest": "' + b'a' * 64 +
                    b'", "host_pid": 123, "op": "observer", "root": "/reviewed/observer", "v": 1}\n')
        assert child.recv(4096) == expected
    finally:
        if child is not None:
            child.close()
        transport.close()
        right.close()


@pytest.mark.parametrize('host_required,granted,expected', [
    (False, False, {'required': False}),
    (False, True, {'required': False}),
    (True, True, {'required': False}),
    (True, False, {'required': True, 'requires': 'host_repo', 'backend': 'remote'}),
])
def test_capsule_requirement_table(monkeypatch, host_required, granted, expected):
    from types import SimpleNamespace
    monkeypatch.setattr('hermes_cli.kanban_capsule_admission._requires_host_repo', lambda task: host_required)
    monkeypatch.setattr('hermes_cli.kanban_capabilities._profile_capabilities',
                        lambda *args: ['host_repo'] if granted else [])
    assert capsule.capsule_requirement(SimpleNamespace(assignee='fixture')) == expected


def test_start_claim_golden_and_type_sensitive_rejection():
    context = {'task_id': 't_golden', 'run_id': 7, 'claim_lock': 'claim-golden', 'workspace': '/capsule'}
    start = {'v': 1, 'op': 'start', **context}
    assert observation_bind._check_start(start, context) == '/capsule'
    for key, value in [('task_id', 't_other'), ('run_id', 8), ('run_id', 7.0),
                       ('claim_lock', 'other'), ('workspace', '/capsule-other')]:
        with pytest.raises(RuntimeError):
            observation_bind._check_start(dict(start, **{key: value}), context)


def test_consumed_marker_private_under_root_umask(tmp_path):
    db = tmp_path / 'board.db'
    db.touch()
    folder = rendezvous.rendezvous_dir(db)
    folder.mkdir(mode=0o700)
    ident = {'db': str(db), 'task_id': 't_golden', 'run_id': 7, 'claim_lock': 'claim-golden'}
    old = os.umask(0o002)
    try:
        rendezvous._consume(ident)
    finally:
        os.umask(old)
    marker = folder / '7.consumed'
    assert folder.stat().st_mode & 0o777 == 0o700
    assert marker.stat().st_mode & 0o777 == 0o600
    assert json.loads(marker.read_text()) == {**ident, 'owner_pid': os.getpid()}
    with pytest.raises(rendezvous.RendezvousError, match='capsule_rendezvous_consumed'):
        rendezvous._consume(ident)
