"""Probe admission and durable terminal courier outcomes (no retry after rejection)."""
import copy
import json
import logging

import pytest

from hermes_cli import fd_headroom_incident as incidents
from hermes_cli import kanban_sync_client as sync
from hermes_cli.fd_headroom_transport import Client
from tests.hermes_cli.test_fd_headroom_delivery import sample


@pytest.fixture
def _admitting_hub_judge():
    """Override the directory's permissive fake: admission must remain real here."""


@pytest.mark.parametrize('outcome', ['rejected', 'failed', 'expired'])
def test_terminal_receipt_latches_once_then_healthy_crossing_rearms(tmp_path, monkeypatch, caplog, outcome):
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    calls = []
    result = {'error': 'agent_owned_block', 'exit_code': 2}
    def call(identity, method, path, body, budget):
        calls.append((method, path, body))
        assert method == 'GET'
        return {'request': {'state': outcome, 'result': result}}
    monkeypatch.setattr(sync, '_bounded_call', call)
    path = tmp_path / 'incident.json'
    with caplog.at_level(logging.ERROR):
        first = incidents.report(sample(), state_path=path, client=Client(), now=10000)
        for wall in (10300, 10600, 13900):
            state = incidents.report(sample(), state_path=path, client=Client(), now=wall)
    assert len(calls) == 1, 'terminal requests must not be reconciled every sample'
    assert state['terminal']['state'] == outcome
    assert state['terminal']['result'] == result
    assert state['envelope'] == first['envelope']
    assert state['incident_started_at'] == first['incident_started_at']
    assert state['error_class'] == 'TerminalRequest'
    def no_identity():
        raise AssertionError('terminal incident must not even initialize delivery')
    monkeypatch.setattr(sync, 'load_identity', no_identity)
    import hermes_state_guard
    monkeypatch.setattr(hermes_state_guard, '_in_test_context', lambda: False)
    unchanged = incidents.report(sample(), state_path=path, now=14000)
    assert unchanged['envelope'] == first['envelope']
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and outcome in errors[0].message and 'agent_owned_block' in errors[0].message
    incidents.report({**sample(), 'triggered': False}, state_path=path, client=Client(), now=14200)
    assert len(calls) == 1
    posted = []
    def missing(identity, method, route, body, budget):
        if method == 'GET':
            raise sync.HubError(404, '{"error":"no such request"}')
        posted.append(body)
        return {}
    monkeypatch.setattr(sync, '_bounded_call', missing)
    next_state = incidents.report(sample(), state_path=path, client=Client(), now=14500)
    assert len(posted) == 1
    assert next_state['incident_started_at'] != first['incident_started_at']
    assert next_state['envelope']['id'] != first['envelope']['id']
    assert next_state['last_terminal']['envelope'] == first['envelope']
    assert next_state['last_terminal']['receipt'] == first['terminal']
    assert json.loads(path.read_text()) == next_state


@pytest.mark.parametrize('outcome', ['rejected', 'failed', 'expired'])
@pytest.mark.parametrize('probe', [False, True])
def test_restart_healthy_sample_rearms_persisted_terminal(tmp_path, monkeypatch, outcome, probe):
    """Exercise the actual checker/reporter/transport, losing all process caches."""
    from unittest.mock import Mock
    from hermes_cli import resource_limits as limits
    import hermes_state_guard
    monkeypatch.setattr(hermes_state_guard, '_in_test_context', lambda: False)
    monkeypatch.setattr(incidents, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    calls = []
    terminal = True
    def wire(identity, method, route, body, budget):
        calls.append((method, route, copy.deepcopy(body)))
        if method == 'POST':
            return {}
        if terminal:
            return {'request': {'state': outcome, 'result': {'error': 'refused'}}}
        raise sync.HubError(404, '{"error":"no such request"}')
    monkeypatch.setattr(sync, '_bounded_call', wire)
    resource = Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1)
    resource.getrlimit.return_value = (1000, 10000)
    monkeypatch.setattr(limits, '_resource', resource)
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    monkeypatch.setattr(limits, '_fd_incident', {})
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_pid', limits.os.getpid())
    monkeypatch.setattr(limits, '_fd_status', {})
    from collections import deque
    monkeypatch.setattr(limits, '_fd_samples', deque())
    monkeypatch.setattr(limits, '_fd_interval', 300)
    count = 600
    monkeypatch.setattr(limits, '_fd_snapshot', lambda soft: (count, {'classes': {'other': count}}))
    def check(now):
        return limits.check_fd_headroom({}, role='gateway', now=now, wall_now=10000 + now,
                                        probe=probe, probe_board='fd-proof' if probe else None)
    first = check(0)
    path = incidents._path(first, probe)
    rejected = json.loads(path.read_text())
    assert rejected['terminal']['state'] == outcome and len(calls) == 1
    # A real restart loses every in-memory hint, not the incident file.
    limits._fd_incident = {}
    limits._fd_status.clear()
    limits._fd_samples.clear()
    limits._fd_last_check = None
    monkeypatch.setattr(incidents, '_producer_token', None)
    count = 100
    check(300)
    assert json.loads(path.read_text()).get('terminal_rearmed') is True
    assert len(calls) == 1, 'healthy rearm must not touch identity or transport'
    terminal = False
    count = 600
    check(600)
    posts = [body for method, _, body in calls if method == 'POST']
    assert len(posts) == 1
    current = json.loads(path.read_text())
    assert current['envelope']['id'] != rejected['envelope']['id']
    assert current['last_terminal'] == {'envelope': rejected['envelope'], 'receipt': rejected['terminal']}
    resource.setrlimit.assert_not_called()


def test_terminal_error_log_bounds_receipt_result(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    result = {'error': 'x' * 5000, 'tail': 'not-in-log'}
    monkeypatch.setattr(sync, '_bounded_call', lambda *args: {
        'request': {'state': 'rejected', 'result': result}})
    with caplog.at_level(logging.ERROR):
        state = incidents.report(sample(), state_path=tmp_path / 'incident.json', client=Client())
    assert state['terminal']['result'] == result, 'retain full receipt in private audit'
    assert 'not-in-log' not in caplog.text
    assert max(len(r.message) for r in caplog.records if r.levelno == logging.ERROR) < 1400


@pytest.mark.parametrize('broken', ['missing_envelope', 'missing_census'])
def test_rearm_failure_is_recorded_without_escape(tmp_path, monkeypatch, broken):
    path = tmp_path / 'incident.json'
    state = {}
    incidents._new_envelope(sample(), state, 10000, False, 'default')
    state.update(terminal={'state': 'rejected', 'result': {'error': 'refused'}}, terminal_rearmed=True,
                 terminal_rearmed_by=[incidents._producer()])
    warning = sample()
    if broken == 'missing_envelope':
        state.pop('envelope')
    else:
        warning.pop('census')
    incidents._save(path, state)
    from unittest.mock import Mock
    client = Mock()
    result = incidents.report(warning, state_path=path, client=client, now=10300)
    assert result['error_class'] == 'KeyError'
    assert result['terminal'] == state['terminal']
    assert json.loads(path.read_text()) == result
    client.create.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.parametrize('outcome', ['pending', 'queued', 'done'])
def test_uncertain_receipt_keeps_frozen_legacy_probe_envelope(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    path = tmp_path / 'incident.json'
    state = {}
    incidents._new_envelope(sample(), state, 10000, True, 'fd-proof')
    state['envelope']['args']['initial_status'] = 'blocked'  # Frozen historical request.
    incidents._save(path, state)
    original = copy.deepcopy(state)
    calls = []
    def call(identity, method, route, body, budget):
        calls.append((method, route, body))
        return {'request': {'state': outcome, 'result': {}}}
    monkeypatch.setattr(sync, '_bounded_call', call)
    for wall in (10300, 10600):
        state = incidents.report(sample(), state_path=path, client=Client(), now=wall,
                                 probe=True, probe_board='fd-proof')
    assert len(calls) == 2 and all(c[0] == 'GET' for c in calls)
    assert state['envelope'] == original['envelope']
    assert not state.get('terminal')


@pytest.mark.parametrize('entry', ['shared', 'cli'])
def test_probe_envelope_passes_real_shared_hub_admission(tmp_path, monkeypatch, entry):
    """No create/admission mocks: exact args enter the hub's shared mutation boundary."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_db_boards import create_board
    from hermes_cli.kanban_db_sync import apply_sync_schema_steps, set_identity
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    board = 'fd-proof'
    create_board(board)
    state = {}
    incidents._new_envelope(sample(), state, 10000, True, board)
    args = state['envelope']['args']
    with connect_closing(board=board) as conn:
        apply_sync_schema_steps(conn)
        set_identity(conn, board, 'hub')
        if entry == 'shared':
            tid = kb.create_task(conn, **args, board=board, created_by='hub:gw:probe')
            assert kb.create_task(conn, **args, board=board, created_by='hub:gw:probe') == tid
        else:
            import argparse
            from hermes_cli import kanban as cli
            from hermes_cli.kanban_db import scoped_current_board
            parser = argparse.ArgumentParser()
            cli.build_parser(parser.add_subparsers())
            argv = ['kanban', '--board', board, 'create', args['title'], '--body', args['body'],
                    '--assignee', args['assignee'], '--idempotency-key', args['idempotency_key'],
                    '--runs-on', ','.join(args['runs_on']), '--created-by', 'hub:gw:probe']
            if 'initial_status' in args:
                argv += ['--initial-status', args['initial_status']]
            with scoped_current_board(board):
                assert cli._cmd_create(parser.parse_args(argv)) == 0
                assert cli._cmd_create(parser.parse_args(argv)) == 0
            tid = conn.execute('SELECT id FROM tasks').fetchone()[0]
        rows = conn.execute('SELECT id,title,status FROM tasks').fetchall()
        assert len(rows) == 1 and rows[0]['id'] == tid
        assert rows[0]['title'].startswith('[probe]') and rows[0]['status'] == 'ready'
    assert 'initial_status' not in args and 'human_request' not in args
    with connect_closing(board='default') as conn:
        assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 0
