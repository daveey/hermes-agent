"""Bounded read-only evidence from an authoritative LOCAL board transaction.

Never use this assembler on a pulled replica. Fetch clocks, pulled_at, HLC and
mtime cannot attest its owner. Local event identities are scoped/non-portable.
No commands, arbitrary event payloads, or event prose enter public observations.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager

from hermes_cli.execution_observations import epoch, human_request_valid, same_json

MAX_HISTORY = 128
_log = logging.getLogger(__name__)
MAX_PAYLOAD = 32768
_TASK_FIELDS = ('id', 'status', 'assignee', 'session_id', 'current_run_id', 'block_kind', 'human_request', 'created_on', 'runs_on')
# Only landing rules traced to the corresponding writers. Ambiguous transitions
# and unstamped release/wake events deliberately do not acquire an identity here.
_FIXED_PHASES = {'dependency_wait': 'todo', 'review_requested': 'review', 'scheduled': 'scheduled',
                 'blocked': 'blocked', 'triage_swept': 'ready', 'claimed': 'running', 'spawned': 'running'}
_PAYLOAD_PHASES = {'created', 'promoted', 'promoted_manual', 'status', 'reopened'}


def _payload(raw):
    if raw is None:
        return {}
    if not isinstance(raw, str) or len(raw) > MAX_PAYLOAD:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _schema(conn):
    return {name: {r[1] for r in conn.execute(f'PRAGMA table_info({name})')}
            for name in ('tasks', 'task_runs', 'task_events', 'task_links', 'kanban_sync_remote_parents')}


@contextmanager
def transaction(conn):
    """Preserve an enclosing transaction, otherwise bind the entire source read."""
    owned = not conn.in_transaction
    at = time.time()
    if owned:
        conn.execute('BEGIN')
    try:
        yield at
    finally:
        if owned:
            conn.execute('COMMIT')


def _rows(conn, sql, args):
    cursor = conn.execute(sql, args)
    cursor.row_factory = sqlite3.Row
    return cursor


def _tasks_runs(conn, chunk, schema):
    marks = ','.join('?' for _ in chunk)
    fields = ','.join((f't.{key}' if key in schema['tasks'] else 'NULL') + f' AS _t_{key}'
                      for key in _TASK_FIELDS)
    if schema['task_runs']:
        run_keys = ('id', 'task_id', 'uid', 'status', 'started_at', 'ended_at', 'last_heartbeat_at')
        run_fields = ','.join((f'r.{key}' if key in schema['task_runs'] else 'NULL') + f' AS {key}' for key in run_keys)
        current_fields = ','.join((f'cr.{key}' if key in schema['task_runs'] else 'NULL') + f' AS _current_{key}' for key in run_keys)
        join = (f'LEFT JOIN task_runs r ON r.task_id=t.id AND r.id IN '
                f'(SELECT id FROM task_runs WHERE task_id=t.id ORDER BY id LIMIT {MAX_HISTORY + 1})')
        current_id = 't.current_run_id' if 'current_run_id' in schema['tasks'] else 'NULL'
        sql = (f'SELECT {fields}, {run_fields}, {current_fields} FROM tasks t {join} '
               f'LEFT JOIN task_runs cr ON cr.id={current_id} AND cr.task_id=t.id WHERE t.id IN ({marks})')
        args = chunk
    else:
        sql, args = f'SELECT {fields} FROM tasks t WHERE t.id IN ({marks})', chunk
    tasks, runs, current = {}, {}, {}
    for row in _rows(conn, sql, args):
        data = dict(row)
        task = {key: data.pop('_t_' + key) for key in _TASK_FIELDS}
        tasks[task['id']] = task
        joined = {key.removeprefix('_current_'): data.pop(key) for key in list(data) if key.startswith('_current_')}
        current[task['id']] = joined if joined.get('id') is not None else None
        if data.get('id') is not None:
            runs.setdefault(task['id'], []).append(data)
    return tasks, runs, current


def _parents(conn, chunk, schema):
    if not schema['task_links']:
        return {}
    marks = ','.join('?' for _ in chunk)
    remote = bool(schema['kanban_sync_remote_parents'])
    status = 'COALESCE(p.status,r.status)' if remote else 'p.status'
    gateway = 'p.created_on' if 'created_on' in schema['tasks'] else 'NULL'
    join = ('LEFT JOIN kanban_sync_remote_parents r ON r.parent_id=l.parent_id' if remote else '')
    rows = _rows(conn, f'SELECT l.child_id, l.parent_id, {status} AS status, p.id AS local_id, {gateway} AS gateway '
                        f'FROM task_links l LEFT JOIN tasks p ON p.id=l.parent_id {join} '
                        f'WHERE l.child_id IN ({marks}) ORDER BY l.parent_id', chunk)
    out = {}
    for row in rows:
        out.setdefault(row['child_id'], []).append({'work_id': row['parent_id'], 'status': row['status'],
                                                  'gateway': row['gateway'],
                                                  'local': row['local_id'] is not None})
    return out


def assemble(conn, ids, *, board, gateway, profile, observed_at):
    """One optional dispatch-snapshot SELECT plus at most 3 SELECTs/500 cards.

    Existing run rows are bounded too: a row ahead of the causal event feed is
    not an old phase observed again. Extra PRAGMAs discover supported old schemas.
    """
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}
    schema = _schema(conn)
    from hermes_cli.kanban_nonrunning_reason import snapshot
    dispatch_observation = snapshot(conn)
    remote_reasons = {}
    if {'uid', 'origin_gateway'} <= schema['task_events']:
        from hermes_cli.kanban_nonrunning_transport import read
        remote_reasons = read(conn, ids, board=board, now=observed_at)
    out = {}
    # The task/run join binds the same chunk twice. Respect the actual connection
    # limit (older SQLite builds default to 999), not only the output batch size.
    chunk_size = min(500, conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) // 2)
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start:start + chunk_size]
        marks = ','.join('?' for _ in chunk)
        tasks, runs, current = _tasks_runs(conn, chunk, schema)
        events = {}
        if schema['task_events']:
            event_fields = ','.join('substr(e.payload,1,32769) AS payload' if key == 'payload' and key in schema['task_events']
                                    else f'e.{key}' if key in schema['task_events'] else f'NULL AS {key}'
                                    for key in ('id', 'task_id', 'run_id', 'kind', 'created_at', 'payload', 'uid'))
            # Indexed per-task LIMITs bound SQLite work as well as returned rows.
            # The full watermark still detects mutations beyond the retained history.
            for row in _rows(conn, f'SELECT {event_fields}, '
                                    f'(SELECT MAX(id) FROM task_events WHERE task_id=t.id) AS _event_watermark, '
                                    f'(SELECT created_at FROM task_events WHERE task_id=t.id '
                                    f'ORDER BY created_at DESC,id DESC LIMIT 1) AS _activity_at, '
                                    f'(SELECT kind FROM task_events WHERE task_id=t.id '
                                    f'ORDER BY created_at DESC,id DESC LIMIT 1) AS _activity_kind '
                                    f'FROM tasks t JOIN task_events e ON e.task_id=t.id AND e.id IN '
                                    f'(SELECT id FROM task_events WHERE task_id=t.id ORDER BY id LIMIT {MAX_HISTORY + 1}) '
                                    f'WHERE t.id IN ({marks})', chunk):
                events.setdefault(row['task_id'], []).append(dict(row))
        parents = _parents(conn, chunk, schema)
        for tid, task in tasks.items():
            out[tid] = {'dispatch_observation': dispatch_observation,
                        'remote_non_running_reason': remote_reasons.get(tid),
                        'events': events.get(tid, []), 'runs': runs.get(tid, []), 'task': task,
                        'current_run': current.get(tid),
                        'event_watermark': events[tid][0]['_event_watermark'] if events.get(tid) else None,
                        'activity_at': events[tid][0]['_activity_at'] if events.get(tid) else None,
                        'activity_kind': events[tid][0]['_activity_kind'] if events.get(tid) else None,
                        'parents': parents.get(tid, []), 'synced': 'uid' in schema['task_events'],
                        'observed_at': observed_at, 'gateway': gateway, 'profile': profile, 'board': board}
    return out


def _event_id(event, synced):
    if not synced:
        return event['id']
    meta = event['_payload'].get('_recovery_history_v1')
    value = meta.get('identity') if isinstance(meta, dict) else event.get('uid')
    # Only a non-empty string is provable (and hashable); anything else is an unprovable identity.
    return value if isinstance(value, str) and value else None


def _tip(events, runs, synced):
    from hermes_cli.kanban_resume_exec import _LIFECYCLE_KINDS
    phases = [e for e in events if e['kind'] in _LIFECYCLE_KINDS]
    if not phases:
        genesis = [e for e in events if e['kind'] == 'created']
        return genesis[0] if len(genesis) == 1 and not runs else None
    if not synced:
        return max(phases, key=lambda e: e['id'])
    by_id = {_event_id(e, True): e for e in phases}
    if None in by_id or len(by_id) != len(phases):
        return None
    ancestors = set()
    for event in phases:
        meta = event['_payload'].get('_recovery_history_v1')
        if (not isinstance(meta, dict) or not isinstance(meta.get('identity'), str)
                or not isinstance(meta.get('predecessors'), list)
                or any(not isinstance(k, str) for k in meta['predecessors'])):
            return None
        ancestors.update(meta['predecessors'])
    tips = set(by_id) - ancestors
    if len(tips) != 1 or ancestors - set(by_id):
        return None
    tip = by_id[tips.pop()]
    # Complete acyclic predecessor closure, not just a unique stamped tip.
    def walk(key, visiting, visited):
        if key in visiting:
            return False
        if key in visited:
            return True
        visiting.add(key)
        predecessors = by_id[key]['_payload']['_recovery_history_v1']['predecessors']
        if not all(walk(k, visiting, visited) for k in predecessors):
            return False
        visiting.remove(key)
        visited.add(key)
        return True
    visited = set()
    if not walk(_event_id(tip, True), set(), visited) or visited != set(by_id):
        return None
    claimed = {e.get('run_id') for e in phases if e['kind'] == 'claimed'}
    if any(r['id'] not in claimed for r in runs):
        return None
    return tip


def _landing(event):
    kind, payload = event['kind'], event['_payload']
    if kind in {'unblocked', 'promoted', 'promoted_manual'}:
        return payload.get('status', 'ready')  # writer omits only for ready -> ready resume
    if kind in _PAYLOAD_PHASES:
        return payload.get('status')
    landing = _FIXED_PHASES.get(kind)
    return landing if payload.get('status', landing) == landing else None


def _generation(event, source):
    if source['synced']:
        key = _event_id(event, True)
        return 'event:' + key if isinstance(key, str) and key else None
    return 'local:' + json.dumps([source[k] for k in ('gateway', 'profile', 'board')] + [event['id']], separators=(',', ':'))


def _wait(result, row, source, event):
    payload, status = event['_payload'], row['status']
    since = result['phase_since']
    target = {**result['identity']}
    target.pop('run_id', None)
    wait = {'owner': dict(result['owner']), 'target': target, 'since': since, 'due_at': None}
    if (status == 'scheduled' and payload.get('clock_only') is True and not payload.get('wake_check')
            and epoch(payload.get('wake_at'))):
        wait.update(kind='time', predicate='clock >= wake_at', due_at=payload['wake_at'])
    elif (status == 'review' and payload.get('reviewer') and payload['reviewer'] == row.get('assignee')
          and event['kind'] == 'review_requested'):
        wait.update(kind='review', predicate='review resolved by completed or changes_requested')
    elif status == 'blocked':
        request = _payload(row.get('human_request')) if isinstance(row.get('human_request'), str) else row.get('human_request')
        if (row.get('block_kind') in {'needs_input', 'capability', 'assignment'}
                and payload.get('kind') == row['block_kind'] and human_request_valid(request)
                and same_json(request, payload.get('human_request'))):
            result['state'] = 'needs_user'
            wait.update(kind=row['block_kind'], predicate='request resolved by unblock')
    elif status == 'todo' and source['parents']:
        parents = source['parents']
        result['dependency_states'] = [{'work_id': p['work_id'], 'status': p['status']} for p in parents]
        if (any(p['status'] not in ('done', 'archived') for p in parents)
                and all(p['local'] and p['gateway'] in (None, '', result['identity']['gateway']) for p in parents)):
            targets = [{k: v for k, v in target.items() if k != 'generation'} | {'work_id': p['work_id']}
                       for p in parents]
            wait.update(kind='dependency', predicate='all named parents in done|archived', targets=targets)
    result['wait'] = wait if wait.get('predicate') else None


def unknown_raw(row, source=None, *, gateway=None, profile=None, board=None):
    """Explicit unknown card observation for an adapter failure; grants no state, clock or authority.

    Keeps the qualified identity (no run/generation) and the row's own human ask with probe fields
    stripped, so an optional adapter failing never erases an outstanding request.
    """
    source = source if isinstance(source, dict) else {}
    gateway = source.get('gateway', gateway)
    profile = source.get('profile', profile)
    board = source.get('board', board)
    board = board if board is not None else row.get('board')
    identity = {'gateway': gateway, 'profile': profile, 'board': board, 'kind': 'card', 'work_id': row.get('id')}
    result = {'version': 1, 'identity': identity, 'owner': {'gateway': gateway, 'profile': profile},
              'state': 'unknown', 'source': 'kanban.card', 'observed_at': None, 'valid_until': None,
              'source_revision': None, 'evidence_refs': [], 'last_progress': None, 'wait': None,
              'last_activity': None, 'last_activity_kind': None, 'unknown_reason': 'adapter_error'}
    request = row.get('human_request')
    request = _payload(request) if isinstance(request, str) else request
    if isinstance(request, dict):
        result['human_request'] = {k: v for k, v in request.items() if k not in ('resolved_check', 'resolved_check_every')}
    return result


def safe_raw(row, source):
    """:func:`raw`, but one card's adapter failure degrades to :func:`unknown_raw`, not its whole read."""
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS
    try:
        return raw(row, source)
    except ADAPTER_ERRORS:
        _log.debug('execution source unavailable for card %s', row.get('id'), exc_info=True)
        task = source.get('task') if isinstance(source, dict) else None
        return unknown_raw({**row, **task} if isinstance(task, dict) else row, source)


def raw(row, source):
    """Execution phase and current non-running reason have independent source clocks."""
    from hermes_cli.kanban_nonrunning_reason import enrich
    value = _phase_raw(row, source)
    if value.get('unknown_reason') == 'owner_observation_unavailable':
        return value
    return enrich(value, {**row, **source['task']}, source)


def _phase_raw(row, source):
    """Strict phase identity and exact wait, distinct from observation/activity/progress."""
    row = {**row, **source['task']}
    gateway, profile, board = (source.get(k) for k in ('gateway', 'profile', 'board'))
    identity = {'gateway': gateway, 'profile': profile, 'board': board, 'kind': 'card', 'work_id': row['id']}
    owner = {'gateway': gateway, 'profile': profile}
    if row.get('assignee'):
        owner['assignee'] = row['assignee']
    if row.get('session_id'):
        owner['session'] = row['session_id']
    at = source['observed_at']
    result = {'version': 1, 'identity': identity, 'owner': owner, 'state': 'unknown',
              'source': 'kanban.card', 'observed_at': at, 'valid_until': at + 180 if epoch(at) else None,
              'source_revision': None, 'evidence_refs': [], 'last_progress': None, 'wait': None,
              'last_activity': source.get('activity_at'), 'last_activity_kind': source.get('activity_kind')}
    request = row.get('human_request')
    request = _payload(request) if isinstance(request, str) else request
    if isinstance(request, dict):
        result['human_request'] = {k: v for k, v in request.items() if k not in ('resolved_check', 'resolved_check_every')}
    if row.get('created_on') and row['created_on'] != gateway:
        result.update(observed_at=None, valid_until=None, unknown_reason='owner_observation_unavailable')
        return result
    events, runs = source['events'], source['runs']
    if len(events) > MAX_HISTORY or len(runs) > MAX_HISTORY:
        result['unknown_reason'] = 'bounded_history_incomplete'
        return result
    if row.get('status') == 'running':
        current = source['current_run']
        if current is None or current.get('status') != 'running' or current.get('ended_at') is not None:
            return result
        uid = current.get('uid')
        if source['synced'] and not uid:
            return result
        identity['run_id'] = uid if uid else current['id']
        heartbeat = current.get('last_heartbeat_at')
        ref = 'task_runs/' + str(identity['run_id']) + '/last_heartbeat_at/' + str(heartbeat)
        result.update(state='running', observed_at=heartbeat, valid_until=heartbeat + 180 if epoch(heartbeat) else None,
                      source_revision=ref, evidence_refs=[ref], generation_portable=bool(uid))
        return result
    parsed = []
    for item in events:
        payload = _payload(item.get('payload'))
        if payload is None:
            # Malformed phase payloads cannot establish or supersede a phase.
            return result
        parsed.append({**item, '_payload': payload})
    event = _tip(parsed, runs, source['synced'])
    if event is None or _landing(event) != row.get('status') or row.get('current_run_id') is not None:
        return result
    generation = _generation(event, source)
    if not generation:
        return result
    # A later local status exit without a lifecycle stamp cannot resurrect a head.
    unbound = {'condition_met', 'continuation_woken', 'wake_check_broken', 'claim_rejected',
               'crashed', 'timed_out', 'stale', 'spawn_failed'}
    if any(e['kind'] in unbound and (source['synced'] or e['id'] > event['id']) for e in parsed):
        return result
    identity['generation'] = generation
    since = event.get('created_at')
    state = {'ready': 'queued', 'scheduled': 'waiting', 'todo': 'waiting', 'review': 'waiting', 'blocked': 'waiting'}.get(row['status'], 'unknown')
    result.update(state=state, source_revision=generation, evidence_refs=[generation],
                  generation_portable=source['synced'], phase_since=since if epoch(since) and since <= at else None)
    if state == 'waiting':
        _wait(result, row, source, event)
    return result
