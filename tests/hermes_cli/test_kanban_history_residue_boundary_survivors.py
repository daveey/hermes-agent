"""Malformed boundary inputs and unmasked historical-custody vetoes.

No production/state mutation: stores and the HTTP peer are existing isolated
fixtures. Refusal tests require Refused, not an arbitrary programming exception.
"""
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_history_residue_custody as custody
from hermes_cli import kanban_history_residue_http as transport
from hermes_cli import kanban_history_residue_local as local
from hermes_cli.kanban_history_residue_source import Refused
from hermes_cli.kanban_sync_client import Hub, Identity
from tests.hermes_cli import test_kanban_history_residue_http as http_helpers
from tests.hermes_cli import test_kanban_history_residue_local as local_helpers
from tests.hermes_cli.test_kanban_history_residue_custody import SCOPE, request

stores = local_helpers.stores
db = http_helpers.db
server = http_helpers.server


@pytest.mark.parametrize('bad_scope', [list(SCOPE), SCOPE[:2], (), (*SCOPE, 'extra'),
                                       ('', 'default', 'home'),
                                       ('https://board.example', ' ', 'home'),
                                       ('https://board.example', 'default', '')],
                         ids=['list', 'short', 'empty', 'long', 'endpoint', 'board', 'gateway'])
def test_custody_scope_rejected_before_schema(db, bad_scope):
    raw, _ = request()
    with pytest.raises(Refused, match='^scope_invalid$'):
        custody.stage_once(db, bad_scope, raw)
    assert db.execute('SELECT name FROM sqlite_master').fetchall() == []


def test_unknown_integer_request_version_never_installs_intent(db):
    raw, _ = request()
    obj = json.loads(raw)
    obj['version'] = 2
    with pytest.raises(Refused, match='^request_invalid$'):
        custody.stage_once(db, SCOPE, json.dumps(obj))
    assert db.execute('SELECT name FROM sqlite_master').fetchall() == []


@pytest.mark.parametrize('version', [7.0, True], ids=['float', 'bool'])
def test_receipt_parent_version_requires_integer_not_numeric_equality(db, version):
    raw, receipt = request()
    obj = json.loads(raw)
    # True == 1: keep every other request/receipt identity check satisfied.
    obj['expected_version'] = 1 if version is True else 7
    raw = json.dumps(obj)
    receipt['parent']['version'] = version
    custody.stage_once(db, SCOPE, raw)
    with pytest.raises(Refused, match='^receipt_request_conflict$'):
        custody.seal(db, SCOPE, 'op-original', json.dumps(receipt))
    assert custody.lookup(db, SCOPE, 'op-original')['receipt_json'] is None


@pytest.mark.parametrize('endpoint', ['http://[::1]', 'https://[::1]', 'http://localhost:443'])
def test_canonical_ipv6_and_nondefault_http_port_are_usable(endpoint):
    hub = SimpleNamespace(ident=Identity(endpoint, 'key', 'home'))
    assert transport.scope(hub, 'default') == (endpoint, 'default', 'home')


@pytest.mark.parametrize('endpoint', ['http://localhost:80', 'https://[::1]:443'])
def test_explicit_default_port_is_an_alias_not_new_custody(endpoint):
    hub = SimpleNamespace(ident=Identity(endpoint, 'key', 'home'))
    with pytest.raises(Refused, match='^endpoint_noncanonical$'):
        transport.scope(hub, 'default')


@pytest.mark.parametrize('endpoint', ['https://', 'ftp://localhost'])
def test_missing_hostname_and_non_http_loopback_fail_as_refusals(db, endpoint):
    raw, _ = request()
    hub = SimpleNamespace(ident=Identity(endpoint, 'key', 'home'))
    with pytest.raises(Refused):
        transport.observe(db, hub, 'default', raw)
    assert db.execute('SELECT name FROM sqlite_master').fetchall() == []


@pytest.mark.parametrize('endpoint', ['https://user@board.example',
                                     'https://:secret@board.example',
                                     'https://board.example?query=1',
                                     'https://board.example#fragment'])
def test_endpoint_credentials_query_fragment_never_get_custody(db, endpoint):
    # Both the identity guard and later exact canonical reconstruction veto
    # these strings. Do not pretend an earlier refusal reason is new authority.
    raw, _ = request()
    hub = SimpleNamespace(ident=Identity(endpoint, 'key', 'home'))
    with pytest.raises(Refused):
        transport.observe(db, hub, 'default', raw)
    assert db.execute('SELECT name FROM sqlite_master').fetchall() == []


@pytest.mark.parametrize('field', ['board', 'gateway', 'key'])
def test_empty_transport_identity_cannot_progress_to_custody(db, field, monkeypatch):
    def forbidden_network(*args):
        pytest.fail('invalid identity reached network')

    monkeypatch.setattr(transport, '_request', forbidden_network)
    board, gateway, key = ('', 'home', 'key') if field == 'board' else (
        ('default', '', 'key') if field == 'gateway' else ('default', 'home', ''))
    hub = SimpleNamespace(ident=Identity('https://board.example', key, gateway))
    with pytest.raises(Refused, match='^endpoint_identity$'):
        transport.current(hub, board, 'op-original')
    assert db.execute('SELECT name FROM sqlite_master').fetchall() == []


def test_nonloopback_request_uses_real_environment_proxy(server, monkeypatch):
    peer, state, _ = server
    monkeypatch.setattr(transport.urllib.request, 'getproxies', lambda: {'http': peer.ident.url})
    monkeypatch.setattr(transport.urllib.request, 'proxy_bypass', lambda host: False)
    resolve = socket.getaddrinfo

    def only_fixture_resolution(host, *args, **kwargs):
        if host == 'history.invalid':
            raise OSError('direct origin resolution forbidden: proxy required')
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', only_fixture_resolution)
    hub = Hub(Identity('http://history.invalid:9', 'key', 'home'))
    try:
        assert transport._request(hub, 'GET', '/history-boundary', None) == b'null'
        assert len(state['calls']) == 1
        assert state['calls'][0][1] == 'http://history.invalid:9/history-boundary'
    finally:
        hub.close()


@pytest.mark.parametrize('column', sorted(sync.JSON_SERVER_COLUMNS['tasks']))
def test_portable_json_metadata_is_decoded_or_refused(stores, column):
    live, _, raw = stores
    live.execute(f'UPDATE tasks SET "{column}"=?', ('{"nested":{"value":1}}',))
    result = local.snapshot(live, raw, board='default', gateway='home', replica=False)
    assert result['portable'][column] == {'nested': {'value': 1}}
    live.execute(f'UPDATE tasks SET "{column}"=?', ('{"duplicate":1,"duplicate":2}',))
    with pytest.raises(Refused, match='^duplicate_key$'):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)


def test_row_count_bound_precedes_any_source_materialization():
    conn = sqlite3.connect(':memory:')
    seen = []
    try:
        conn.execute('CREATE TABLE cells(value TEXT)')
        conn.executemany('INSERT INTO cells VALUES (?)', [('x',)] * (local.MAX_ROWS + 1))

        def record(cursor, row):
            seen.append(tuple(c[0] for c in cursor.description))
            return row

        conn.row_factory = record
        with pytest.raises(Refused, match='^local_snapshot_bound$'):
            local._rows(conn, 'SELECT value FROM cells')
        assert len(seen) == 1  # aggregate only; no raw cell was materialized
        assert seen[0] != ('value',)
    finally:
        conn.close()


@pytest.mark.parametrize('fault', ['duplicate-state', 'wrong-gateway', 'wrong-mode'])
def test_sync_identity_clauses_are_independent(stores, fault):
    live, _, raw = stores
    sql = {'duplicate-state': 'INSERT INTO kanban_sync_state SELECT * FROM kanban_sync_state',
           'wrong-gateway': "UPDATE kanban_sync_state SET gateway_id='elsewhere'",
           'wrong-mode': "UPDATE kanban_sync_state SET mode='shadow'"}[fault]
    live.execute(sql)
    with pytest.raises(Refused, match='^local_sync_identity$'):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)


@pytest.mark.parametrize('column', ['current_run_id', 'claim_lock', 'claim_expires', 'worker_pid',
                                    'worker_started_at', 'lease_holder', 'lease_expires'])
def test_missing_executor_column_is_refused_not_a_keyerror(stores, column):
    live, _, raw = stores
    live.execute(f'ALTER TABLE tasks DROP COLUMN "{column}"')
    with pytest.raises(Refused, match='^local_executor_active$'):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)


@pytest.mark.parametrize('status,ended', [('done', None), ('running', 123)])
def test_run_completion_requires_both_terminal_status_and_end(stores, status, ended):
    live, _, raw = stores
    live.execute('INSERT INTO task_runs VALUES (1,?,?,?,?)', ('run', 't_stopped', status, ended))
    with pytest.raises(Refused, match='^local_running_run$'):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)


@pytest.mark.parametrize('state', ['rejected', 'pending'])
def test_other_event_work_does_not_alias_selected_source(stores, state):
    live, _, raw = stores
    source = json.loads(raw)
    other = dict(source, op_id='op-second', row_uid='event-second', state=state)
    live.execute('INSERT INTO kanban_sync_outbox VALUES (' + ','.join('?' for _ in other) + ')',
                 tuple(other.values()))
    # fields.task_id selects both ops, but only event-original identifies source.
    if state == 'pending':
        with pytest.raises(Refused, match='^local_uncovered_work$'):
            local.snapshot(live, raw, board='default', gateway='home', replica=False)
    else:
        result = local.snapshot(live, raw, board='default', gateway='home', replica=False)
        assert [op['op_id'] for op in result['ops']] == ['op-original', 'op-second']


def test_same_uid_in_wrong_table_is_never_accepted(stores):
    live, _, raw = stores
    source = json.loads(raw)
    other = dict(source, op_id='op-second', table_name='tasks')
    live.execute('INSERT INTO kanban_sync_outbox VALUES (' + ','.join('?' for _ in other) + ')',
                 tuple(other.values()))
    # For C229 the source filter rejects earlier; intact code reaches the
    # uncovered-table veto. Both must refuse, not produce an audit snapshot.
    with pytest.raises(Refused):
        local.snapshot(live, raw, board='default', gateway='home', replica=False)
