"""Bounded persisted-source correlation, NOT admission or lifecycle authority.

Only reads SQLite; no gateway state constructor, custody restoration, or network.
Persisted admission/tool records are independent correlation, not signed proof.
Returned rows retain every original column (including BLOB bytes).
"""
from __future__ import annotations

import hashlib
from itertools import pairwise
import json
import math
from pathlib import Path
import sqlite3

from hermes_cli.kanban_retain_transport import canonical

MAX_CANDIDATES = 20
MAX_MESSAGES = 2000
MAX_BYTES = 4 * 1024 * 1024
REJECTION = 'retain rejected: immutable custody retained; ordinary failure recovery owns this run'


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _json(text):
    return json.loads(text, object_pairs_hook=_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _size(row):
    return sum(len(v) if isinstance(v, bytes) else len(str(v).encode('utf-8'))
               for v in row.values() if v is not None)


def _command(value, op, wire):
    expected = {'op_id': wire['client_op_id'], 'request_uid': wire['client_op_id'],
                'run_uid': wire['run_uid'], 'task_id': wire['task'],
                'event_uid': wire['event_uid'], 'operation_kind': 'retain',
                'delivery_state': 'rejected', 'canonical_receipt_json': None,
                'rejected_release_op_id': None, 'request_json': canonical(wire),
                'request_sha256': hashlib.sha256(canonical(wire).encode('utf-8')).hexdigest()}
    return isinstance(value, dict) and all(k in value and value[k] == v for k, v in expected.items()) and all(
        k in op and op[k] == v for k, v in expected.items())


def _ordered(*messages):
    return all(a['id'] < b['id'] and a['timestamp'] < b['timestamp']
               for a, b in pairwise(messages))


def _source(admission, session, messages, board, op, wire):
    payload = _json(admission['payload_json'])
    card = payload['kanban_card_v1']
    run = card['run_id']
    request = 'kanban:' + json.dumps([board, wire['task'], run], separators=(',', ':'))
    if (payload.get('finite') is not True or type(run) is not int or run <= 0
            or card['board'] != board or card['task_id'] != wire['task']
            or admission['request_id'] != request or admission['status'] != 'terminal'
            or not admission['admission_id'] or not admission['principal_id']
            or admission['principal_id'] != session['user_id']
            or session['source'] != 'kanban' or session['created_source'] != 'kanban'
            or admission['target_session_id'] != session['id']
            or type(session['ended_at']) not in (int, float)
            or not math.isfinite(session['ended_at'])
            or session['ended_at'] <= session['started_at']):
        return []
    shows, calls, results, commands = _messages(session, messages, request, run, op, wire)
    return _matches(admission, session, shows, calls, results, commands)


def _messages(session, messages, request, run, op, wire):
    shows, calls, results, commands = [], [], [], []
    for message in messages:
        if (message['session_id'] != session['id'] or type(message['id']) is not int
                or type(message['timestamp']) not in (int, float)
                or not math.isfinite(message['timestamp'])
                or not session['started_at'] <= message['timestamp'] <= session['ended_at']):
            raise ValueError('invalid source message')
        if message['role'] == 'assistant' and message['tool_calls']:
            entries = _json(message['tool_calls'])
            if not isinstance(entries, list):
                raise ValueError('invalid tool calls')
            for entry in entries:
                fn = entry['function']
                if fn['name'] == 'kanban_retain':
                    args = _json(fn['arguments'])
                    call_id = entry.get('id')
                    if (call_id and entry.get('call_id', call_id) == call_id
                            and args == {k: v for k, v in wire['payload'].items() if k != 'version'}):
                        calls.append((message, call_id))
        if message['role'] != 'tool':
            continue
        name = message['tool_name']
        if name not in ('kanban_show', 'kanban_retain', 'terminal'):
            continue
        try:
            value = _json(message['content'])
        except json.JSONDecodeError:
            if name == 'terminal':
                continue  # Ordinary terminal notices are not structured evidence.
            raise
        if name == 'kanban_show':
            task = value['task']
            events = value['events']
            claimed = any(e['kind'] == 'claimed' and e['run_id'] == run
                          and isinstance(e.get('payload'), dict)
                          and e['payload'].get('run_uid') == wire['run_uid']
                          and e['created_at'] <= message['timestamp'] for e in events)
            admitted = any(e['kind'] == 'owner_admitted' and e['run_id'] == run
                           and e['payload'].get('request_id') == request
                           and e['payload'].get('session_id') == session['id']
                           and e['created_at'] <= message['timestamp'] for e in events)
            if task['id'] == wire['task'] and task['current_run_id'] == run and claimed and admitted:
                shows.append(message)
        elif name == 'kanban_retain':
            if value.get('ok') is False and value.get('terminal_for_run') is False and value.get('error') == REJECTION:
                results.append(message)
        elif value.get('exit_code') == 0 and value.get('error') is None:
            try:
                rows = _json(value.get('output', ''))
            except (ValueError, TypeError):
                continue  # Most terminal output is not JSON command evidence.
            if isinstance(rows, list) and len(rows) == 1 and _command(rows[0], op, wire):
                commands.append((message, rows[0]))
    return shows, calls, results, commands


def _matches(admission, session, shows, calls, results, commands):
    # Refuse dense/ambiguous histories rather than doing a Cartesian scan.
    if len(shows) * len(calls) * len(results) * len(commands) > MAX_MESSAGES:
        return []
    matches = []
    for show in shows:
        for call, call_id in calls:
            for result in results:
                for evidence, command in commands:
                    if result['tool_call_id'] == call_id and _ordered(show, call, result, evidence):
                        matches.append({'kind': 'historical_rejected_source', 'lifecycle_authority': False,
                                        'admission': admission, 'session': session, 'command': command,
                                        'messages': [show, call, result, evidence]})
                        if len(matches) > 1:
                            return matches
    return matches


def discover(state_path: Path, board: str, op: dict, wire: dict) -> dict | None:
    """Return one unique historical source, else None; fail closed on bounds/schema.

    Re-run and compare the complete object after external reads to revalidate.
    No observed_at or current run interpretation is included. BLOBs remain bytes.
    """
    conn = None
    try:
        if (not isinstance(board, str) or not board or wire['op'] != 'retain'
                or type(wire['payload']['version']) is not int or wire['payload']['version'] != 1
                or not _command(op, op, wire)):
            return None
        conn = sqlite3.connect(Path(state_path).resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_BYTES)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')  # One consistent read snapshot across all selected rows.
        conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_OK if action in (
            sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY)
        # A VM budget also bounds scans on older state schemas without JSON indexes.
        budget = [10000]
        def progress():
            budget[0] -= 1
            return budget[0] <= 0
        conn.set_progress_handler(progress, 1000)
        admissions = conn.execute("""SELECT * FROM session_admissions
            WHERE CASE WHEN json_valid(payload_json) THEN
            json_extract(payload_json,'$.kanban_card_v1.board')=?
            AND json_extract(payload_json,'$.kanban_card_v1.task_id')=? ELSE 0 END LIMIT ?""",
                                  (board, wire['task'], MAX_CANDIDATES + 1)).fetchall()
        if len(admissions) > MAX_CANDIDATES:
            return None
        matches, used = [], 0
        for row in admissions:
            admission = dict(row)
            session_rows = conn.execute('SELECT * FROM sessions WHERE id=? LIMIT 2',
                                        (admission['target_session_id'],)).fetchall()
            if len(session_rows) != 1:
                return None
            session = dict(session_rows[0])
            used += _size(admission) + _size(session)
            messages = []
            cursor = conn.execute('SELECT * FROM messages WHERE session_id=? ORDER BY id LIMIT ?',
                                  (session['id'], MAX_MESSAGES + 1))
            for message in cursor:
                message = dict(message)
                used += _size(message)
                messages.append(message)
                if used > MAX_BYTES or len(messages) > MAX_MESSAGES:
                    return None
            try:
                candidate = _source(admission, session, messages, board, op, wire)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                candidate = []  # Malformed records cannot supply a matching source.
            matches.extend(candidate)
            if len(matches) > 1:
                return None
        return matches[0] if len(matches) == 1 else None
    except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError, RecursionError):
        return None
    finally:
        if conn is not None:
            conn.close()
