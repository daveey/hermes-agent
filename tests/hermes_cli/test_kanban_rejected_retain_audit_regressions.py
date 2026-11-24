"""Adjacent-valid mutation controls for historical custody guards."""
import copy
import json
import sqlite3

import pytest

from hermes_cli.kanban_retain_transport import canonical
from tests.hermes_cli import test_kanban_rejected_retain_source as helpers
from tests.hermes_cli import test_kanban_rejected_retain_history as history_tests


@pytest.mark.parametrize('kind', ['assistant', 'claim_payload', 'output_object',
                                 'output_scalar', 'command_scalar', 'command_missing'])
def test_irrelevant_records_do_not_veto_exact_source(tmp_path, kind):
    from hermes_cli import kanban_rejected_retain_source as source
    _, op, wire, admission, session, messages = helpers.fixture_db(tmp_path)
    if kind == 'claim_payload':
        show = json.loads(messages[0]['content'])
        extra = copy.deepcopy(show['events'][0])
        extra['payload'] = None
        show['events'].insert(0, extra)
        messages[0]['content'] = canonical(show)
    else:
        extra = copy.deepcopy(messages[-1])
        extra.update(id=50, timestamp=50.0)
        if kind == 'assistant':
            extra.update(role='assistant', tool_calls=None)
        else:
            value = {'output_object': {'unrelated': 1}, 'output_scalar': 1,
                     'command_scalar': [1], 'command_missing': [{'unrelated': 1}]}[kind]
            extra['content'] = canonical({'exit_code': 0, 'error': None,
                                          'output': canonical(value)})
        messages.append(extra)
    assert len(source._source(admission, session, messages, 'default', op, wire)) == 1


@pytest.mark.parametrize('name', ['kanban_show', 'kanban_retain'])
def test_non_json_structured_tool_record_is_not_an_ordinary_notice(tmp_path, name):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, *_ = helpers.fixture_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO messages SELECT 50,session_id,role,?,?,'
                     'tool_call_id,tool_calls,50,display_identity FROM messages WHERE id=40',
                     (name, 'not JSON structured tool evidence'))
    assert source.discover(path, 'default', op, wire) is None


def test_irrelevant_nonfinite_message_cannot_supply_a_history(tmp_path):
    from hermes_cli import kanban_rejected_retain_source as source
    _, op, wire, admission, session, messages = helpers.fixture_db(tmp_path)
    session['started_at'] = float('-inf')
    extra = copy.deepcopy(messages[0])
    extra.update(id=1, timestamp=float('-inf'), role='user')
    with pytest.raises(ValueError, match='invalid source message'):
        source._source(admission, session, [extra, *messages], 'default', op, wire)



@pytest.mark.parametrize('bad', ['{}', '""'])
def test_nonlist_tool_calls_fail_closed(tmp_path, bad):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, *_ = helpers.fixture_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO messages SELECT 50,session_id,role,tool_name,content,'
                     'tool_call_id,?,50,display_identity FROM messages WHERE id=20', (bad,))
    assert source.discover(path, 'default', op, wire) is None


def test_missing_target_session_is_not_an_index_error(tmp_path):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, *_ = helpers.fixture_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM sessions')
    assert source.discover(path, 'default', op, wire) is None


def test_byte_budget_counts_blob_bytes_and_omits_nulls(tmp_path, monkeypatch):
    from hermes_cli import kanban_rejected_retain_source as source
    path, op, wire, admission, session, messages = helpers.fixture_db(tmp_path)
    expected = sum(len(v) if isinstance(v, bytes) else len(str(v).encode('utf-8'))
                   for row in [admission, session, *messages] for v in row.values() if v is not None)
    monkeypatch.setattr(source, 'MAX_BYTES', expected)
    assert source.discover(path, 'default', op, wire) is not None
    monkeypatch.setattr(source, 'MAX_BYTES', expected - 1)
    assert source.discover(path, 'default', op, wire) is None


@pytest.mark.parametrize('board,version,operation', [
    (5, 1, 'retain'), ('', 1, 'retain'), ('default', True, 'retain'),
    ('default', 1.0, 'retain'), ('default', 2, 'retain'), ('default', 1, 'release'),
])
def test_discovery_input_veto_with_consistent_correlated_records(tmp_path, board, version, operation):
    from hermes_cli import kanban_rejected_retain_history as history
    from hermes_cli import kanban_rejected_retain_source as source
    path, _, wire, admission, _, messages = helpers.fixture_db(tmp_path)
    wire['op'] = operation
    wire['payload']['version'] = version
    op = history._command(wire)
    payload = json.loads(admission['payload_json'])
    payload['kanban_card_v1']['board'] = board
    request = 'kanban:' + json.dumps([board, wire['task'], 42], separators=(',', ':'))
    show = json.loads(messages[0]['content'])
    show['events'][1]['payload']['request_id'] = request
    messages[0]['content'] = canonical(show)
    messages[3]['content'] = canonical({'exit_code': 0, 'error': None, 'output': canonical([op])})
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE session_admissions SET payload_json=?,request_id=?', (canonical(payload), request))
        for message in (messages[0], messages[3]):
            conn.execute('UPDATE messages SET content=? WHERE id=?', (message['content'], message['id']))
    assert source.discover(path, board, op, wire) is None


@pytest.mark.parametrize('table,key,value', [
    ('tasks', 'head', -1), ('task_runs', 'head', -1),
    ('tasks', 'floor', -1), ('task_runs', 'floor', True),
    ('tasks', 'floor', 1.5), ('task_events', 'floor', False),
    ('tasks', 'tombstone', {}), ('task_runs', 'tombstone', {}),
    ('task_events', 'row', {}), ('tasks', 'row', []), ('task_runs', 'row', []),
])
def test_remote_shape_and_floor_veto(tmp_path, table, key, value):
    from hermes_cli import kanban_rejected_retain_history as history
    _, conn, op, _, path, _, hub = history_tests.case.__wrapped__(tmp_path)
    original = hub.row_evidence
    def evidence(board, name, uid):
        result = original(board, name, uid)
        if name == table:
            result[key] = value
        return result
    hub.row_evidence = evidence
    assert history.retire(conn, hub, 'default', op, path)['action'] == 'retained'
    assert conn.execute('SELECT count(*) FROM kanban_sync_outbox').fetchone()[0] == 1


@pytest.mark.parametrize('table', ['tasks', 'task_runs', 'task_events'])
def test_nondict_remote_evidence_is_retained(tmp_path, table):
    from hermes_cli import kanban_rejected_retain_history as history
    _, conn, op, _, path, _, hub = history_tests.case.__wrapped__(tmp_path)
    original = hub.row_evidence
    hub.row_evidence = lambda board, name, uid: [] if name == table else original(board, name, uid)
    assert history.retire(conn, hub, 'default', op, path)['action'] == 'retained'


def test_noncanonical_wire_bytes_cannot_be_archived(tmp_path):
    from hermes_cli import kanban_rejected_retain_history as history
    _, conn, op, _, path, _, hub = history_tests.case.__wrapped__(tmp_path)
    op['fields'] += ' '
    conn.execute('UPDATE kanban_sync_outbox SET fields=?', (op['fields'],))
    conn.commit()
    assert history.retire(conn, hub, 'default', op, path)['action'] == 'retained'
