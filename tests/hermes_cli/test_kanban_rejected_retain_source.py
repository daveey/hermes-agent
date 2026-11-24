"""Read-only historical-source correlation, never lifecycle authority."""
import hashlib
import json
import sqlite3

import pytest

from hermes_cli.kanban_retain_transport import canonical


def fixture_db(tmp_path):
    wire = {'op': 'retain', 'client_op_id': 'op-1', 'task': 'task-1',
            'run_uid': 'run-1', 'event_uid': 'event-1', 'expected_episode': None,
            'payload': {'version': 1, 'reason': 'decision_pending', 'summary': 'é'}}
    op = {'op_id': 'op-1', 'request_uid': 'op-1', 'task_id': 'task-1',
          'run_uid': 'run-1', 'event_uid': 'event-1', 'operation_kind': 'retain',
          'delivery_state': 'rejected', 'canonical_receipt_json': None,
          'rejected_release_op_id': None, 'request_json': canonical(wire),
          'request_sha256': hashlib.sha256(canonical(wire).encode()).hexdigest()}
    request = 'kanban:["default","task-1",42]'
    admission = {'seq': 1, 'admission_id': 'admit-1', 'request_id': request,
                 'principal_id': 'uid:501', 'target_session_id': 's1',
                 'payload_json': canonical({'finite': True, 'kanban_card_v1': {
                     'board': 'default', 'task_id': 'task-1', 'run_id': 42}}),
                 'status': 'terminal'}
    session = {'id': 's1', 'source': 'kanban', 'created_source': 'kanban',
               'user_id': 'uid:501', 'started_at': 1.0, 'ended_at': 100.0}
    show = {'task': {'id': 'task-1', 'current_run_id': 42}, 'events': [
        {'kind': 'claimed', 'run_id': 42, 'created_at': 2,
         'payload': {'run_uid': 'run-1'}},
        {'kind': 'owner_admitted', 'run_id': 42, 'created_at': 3,
         'payload': {'request_id': request, 'session_id': 's1'}}]}
    def message(i, role, name=None, content=None, call=None, calls=None):
        return {'id': i, 'session_id': 's1', 'role': role, 'tool_name': name,
                'content': content, 'tool_call_id': call, 'tool_calls': calls,
                'timestamp': float(i), 'display_identity': b'\x00\xff'}
    def toolcall(i, name, args):
        return canonical([{'id': i, 'call_id': i, 'function': {
            'name': name, 'arguments': canonical(args)}}])
    messages = [message(10, 'tool', 'kanban_show', canonical(show), 'show-call'),
                message(20, 'assistant', calls=toolcall('retain-call', 'kanban_retain',
                        {'reason': 'decision_pending', 'summary': 'é'})),
                message(30, 'tool', 'kanban_retain', canonical({'ok': False,
                    'terminal_for_run': False, 'error': 'retain rejected: immutable custody retained; ordinary failure recovery owns this run'}), 'retain-call'),
                message(40, 'tool', 'terminal', canonical({'output': json.dumps([op]),
                         'exit_code': 0, 'error': None}), 'sql-call')]
    path = tmp_path / 'state.db'
    with sqlite3.connect(path) as conn:
        for table, rows in [('sessions', [session]), ('session_admissions', [admission]), ('messages', messages)]:
            conn.execute('CREATE TABLE ' + table + ' (' + ','.join(
                '"' + k + '" ' + ('INTEGER PRIMARY KEY' if k in ('seq',) or (table == 'messages' and k == 'id') else 'BLOB') for k in rows[0]) + ')')
            for row in rows:
                conn.execute('INSERT INTO ' + table + ' VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
    return path, op, wire, admission, session, messages


def test_discover_preserves_exact_source_and_never_changes_database(tmp_path):
    from hermes_cli.kanban_rejected_retain_source import discover
    path, op, wire, admission, session, messages = fixture_db(tmp_path)
    before = path.read_bytes()
    result = discover(path, 'default', op, wire)
    assert result is not None
    assert result['kind'] == 'historical_rejected_source'
    assert result['lifecycle_authority'] is False
    assert result['admission'] == admission
    assert result['session'] == session
    assert result['messages'] == messages
    assert result['command'] == op
    assert discover(path, 'default', op, wire) == result
    assert path.read_bytes() == before


@pytest.mark.parametrize('table,key,value', [
    ('sessions', 'source', 'cli'),
    ('sessions', 'created_source', 'cli'),
    ('sessions', 'user_id', 'uid:502'),
    ('sessions', 'ended_at', None),
    ('sessions', 'ended_at', 1.0),
    ('session_admissions', 'status', 'active'),
    ('session_admissions', 'admission_id', ''),
    ('session_admissions', 'request_id', 'kanban:["default","task-1",43]'),
    ('session_admissions', 'principal_id', 'uid:502'),
    ('session_admissions', 'payload_json', '{"finite":false,"kanban_card_v1":{"board":"default","task_id":"task-1","run_id":42}}'),
    ('session_admissions', 'payload_json', '{"finite":true,"kanban_card_v1":{"board":"default","task_id":"task-1","run_id":true}}'),
])
def test_admission_session_guards(tmp_path, table, key, value):
    from hermes_cli.kanban_rejected_retain_source import discover
    path, op, wire, *_ = fixture_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE {table} SET {key}=?', (value,))
    assert discover(path, 'default', op, wire) is None


@pytest.mark.parametrize('defect', [
    'show-task', 'show-run', 'claim-run', 'owner-request', 'owner-session',
    'claim-time', 'missing-claim', 'missing-admitted', 'call-name', 'call-args',
    'call-id', 'result-id', 'result-name', 'result-error', 'result-ok',
    'result-terminal', 'result-time', 'evidence-time', 'evidence-role',
    'evidence-name', 'evidence-exit', 'evidence-error', 'evidence-text',
    'command-hash', 'command-bytes', 'command-receipt', 'command-release',
    'command-task', 'command-kind', 'command-state', 'command-event',
    'duplicate-json', 'duplicate-call', 'duplicate-result', 'duplicate-source',
    'message-overflow', 'byte-overflow', 'admission-overflow',
])
def test_independent_evidence_guards(tmp_path, defect):
    from hermes_cli.kanban_rejected_retain_source import discover
    path, op, wire, _admission, _, messages = fixture_db(tmp_path)
    show, call, result, evidence = messages
    sv, cv, rv, ev = (json.loads(m['content'] or m['tool_calls']) for m in messages)
    command = json.loads(ev['output'])[0]
    changes = {
        'show-task': (sv['task'], 'id', 'foreign'),
        'show-run': (sv['task'], 'current_run_id', 43),
        'claim-run': (sv['events'][0]['payload'], 'run_uid', 'foreign'),
        'owner-request': (sv['events'][1]['payload'], 'request_id', 'foreign'),
        'owner-session': (sv['events'][1]['payload'], 'session_id', 'foreign'),
        'claim-time': (sv['events'][0], 'created_at', 21),
        'call-name': (cv[0]['function'], 'name', 'kanban_comment'),
        'call-args': (cv[0]['function'], 'arguments', '{"summary":"foreign"}'),
        'call-id': (cv[0], 'call_id', 'different'),
        'result-id': (result, 'tool_call_id', 'foreign'),
        'result-name': (result, 'tool_name', 'terminal'),
        'result-error': (rv, 'error', 'temporarily unavailable'),
        'result-ok': (rv, 'ok', True),
        'result-terminal': (rv, 'terminal_for_run', True),
        'result-time': (result, 'timestamp', 19),
        'evidence-time': (evidence, 'timestamp', 29),
        'evidence-role': (evidence, 'role', 'assistant'),
        'evidence-name': (evidence, 'tool_name', 'read_file'),
        'evidence-exit': (ev, 'exit_code', 1),
        'evidence-error': (ev, 'error', 'failure'),
        'command-hash': (command, 'request_sha256', '0' * 64),
        'command-bytes': (command, 'request_json', command['request_json'] + ' '),
        'command-receipt': (command, 'canonical_receipt_json', '{}'),
        'command-release': (command, 'rejected_release_op_id', 'release'),
        'command-task': (command, 'task_id', 'foreign'),
        'command-kind': (command, 'operation_kind', 'failure'),
        'command-state': (command, 'delivery_state', 'unknown'),
        'command-event': (command, 'event_uid', 'foreign'),
    }
    if defect in changes:
        target, key, value = changes[defect]
        target[key] = value
    if defect == 'missing-claim': sv['events'].pop(0)
    if defect == 'missing-admitted': sv['events'].pop()
    show['content'], call['tool_calls'], result['content'] = json.dumps(sv), json.dumps(cv), json.dumps(rv)
    ev['output'] = json.dumps([command])
    evidence['content'] = json.dumps(ev)
    if defect == 'evidence-text': evidence['content'] = 'Quoted command: ' + evidence['content']
    if defect == 'duplicate-json': result['content'] = result['content'][:-1] + ',"ok":false}'
    if defect == 'duplicate-call':
        cv.append(cv[0]); call['tool_calls'] = json.dumps(cv)
    with sqlite3.connect(path) as conn:
        for m in messages:
            conn.execute('UPDATE messages SET ' + ','.join(k + '=?' for k in m if k != 'id') + ' WHERE id=?',
                         tuple(v for k, v in m.items() if k != 'id') + (m['id'],))
        if defect == 'duplicate-result':
            conn.execute('INSERT INTO messages SELECT 31,session_id,role,tool_name,content,tool_call_id,tool_calls,31,display_identity FROM messages WHERE id=30')
        if defect in ('duplicate-source', 'admission-overflow'):
            for i in range(2, 3 if defect == 'duplicate-source' else 23):
                conn.execute('INSERT INTO session_admissions SELECT ?,admission_id,request_id,principal_id,target_session_id,payload_json,status FROM session_admissions WHERE seq=1', (i,))
        if defect == 'message-overflow':
            for i in range(2001):
                conn.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)', (i+100, 's1', 'user', None, '', None, None, 50.0, b''))
        if defect == 'byte-overflow':
            conn.execute('UPDATE messages SET display_identity=? WHERE id=20', (b'x' * (4 * 1024 * 1024),))
    before = path.read_bytes()
    assert discover(path, 'default', op, wire) is None, defect
    assert path.read_bytes() == before


@pytest.mark.parametrize('message_id,key,value', [
    (10, 'content', '[]'), (10, 'content', '{"task":null}'),
    (20, 'tool_calls', '[null]'), (20, 'tool_calls', '[{"function":null}]'),
    (30, 'content', '[]'), (40, 'content', '[]'),
])
def test_malformed_structured_records_fail_closed(tmp_path, message_id, key, value):
    from hermes_cli.kanban_rejected_retain_source import discover
    path, op, wire, *_ = fixture_db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(f'UPDATE messages SET {key}=? WHERE id=?', (value, message_id))
    assert discover(path, 'default', op, wire) is None


def test_missing_database_does_not_create_file(tmp_path):
    from hermes_cli.kanban_rejected_retain_source import discover
    _, op, wire, *_ = fixture_db(tmp_path)
    path = tmp_path / 'absent.db'
    assert discover(path, 'default', op, wire) is None
    assert not path.exists()
