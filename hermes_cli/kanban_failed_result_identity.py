"""Portable causal evidence for modern failed worker results."""
from __future__ import annotations

import json
from typing import cast


def owner_restart_identity_fields(conn, context, task):
    """Witness an owner booking against the actual recorded outer launch.

    No observer is started here: an already armed child observer sees the new
    result; owner-only bookings rely on the dispatcher's next sweep.
    """
    from hermes_cli.kanban_failed_result import valid_birth
    params = (context['task_id'], context['run_id'])
    birth = conn.execute('SELECT worker_started_at FROM tasks WHERE id=?', (task.id,)).fetchone()[0]
    spawns = conn.execute("SELECT * FROM task_events WHERE task_id=? AND run_id=? "
                          "AND kind='spawned' ORDER BY id DESC", params).fetchall()
    for event in spawns:
        payload = _payload(event)
        if (payload is not None and type(payload.get('pid')) is int
                and payload['pid'] == task.worker_pid
                and payload.get('started_at') == birth
                and valid_birth(birth)):
            from hermes_cli.kanban_lost_result import evidence_rows
            rows = evidence_rows(conn, *params)
            sequences = [p['result_sequence'] for row in rows
                         if (p := _payload(row)) and type(p.get('result_sequence')) is int]
            return {'death_proof_version': 1, 'result_role': 'owner_restart',
                    'outer_event_identity': _identity(event, payload),
                    'result_sequence': max([0, *sequences]) + 1,
                    'causal_predecessors': [_event_identity(row) for row in rows]}
    return {}


def _owner_restart_events(conn, task_id, run_id, rows, results):
    """Only a complete causal snapshot and witnessed outer identity authorize 76."""
    from hermes_cli.kanban_failed_result import valid_birth
    for event, payload in results:
        if (payload is None or payload.get('death_proof_version') != 1
                or payload.get('result_role') != 'owner_restart'
                or payload.get('owner_restart') is not True
                or type(payload.get('exit_code')) is not int or payload['exit_code'] != 76
                or type(payload.get('result_sequence')) is not int or payload['result_sequence'] <= 0):
            continue
        prior = payload.get('causal_predecessors')
        if (not isinstance(prior, list) or not all(isinstance(item, str) for item in prior)
                or sorted(prior) != sorted(_event_identity(row) for row in rows if row['id'] != event['id'])):
            continue
        identity = payload.get('outer_event_identity')
        spawns = conn.execute("SELECT * FROM task_events WHERE task_id=? AND run_id=? "
                              "AND kind='spawned'", (task_id, run_id)).fetchall()
        matches = [(row, p) for row in spawns if (p := _payload(row)) is not None
                   and _identity(row, p) == identity]
        if (len(matches) == 1 and type(payload.get('pid')) is int and payload['pid'] > 0
                and payload['pid'] == matches[0][1].get('pid')
                and valid_birth(matches[0][1].get('started_at'))):
            return matches[0], (event, payload), True
    return None


def modern_receipt(conn, task_id, run_id, claim_lock):
    """Return (modern, selected result event/payload), including a strict veto.

    Classifiers also run after closure, so this checks receipt attribution, not
    task liveness. A modern but ambiguous history must never use legacy logs.
    """
    from hermes_cli.kanban_failed_result import modern_result_recorded
    if not modern_result_recorded(conn, task_id, run_id):
        return False, None
    selected = attributable_events(conn, task_id, run_id)
    if selected is None:
        return True, None
    (_, bound), result, _ = selected
    payload = result[1]
    owner = payload is not None and payload.get('result_role') == 'owner_restart'
    if (bound is None or payload is None or (not owner and bound.get('claim_lock') != claim_lock)
            or payload.get('claim_lock') != claim_lock
            or bound.get('pid') is None or payload.get('pid') != bound.get('pid')):
        return True, None
    return True, (result[0], payload)


def _payload(event):
    try:
        value = json.loads(event['payload'])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _identity(event, payload):
    value = {key: payload.get(key) for key in ('pid', 'started_at', 'claim_lock')}
    if 'uid' in event.keys() and event['uid']:
        value['uid'] = event['uid']
    return value


def _event_identity(event):
    if 'uid' in event.keys() and event['uid']:
        return event['uid']
    import hashlib
    # Unsynced boards lack UIDs; hash exact content instead of local integers.
    return hashlib.sha256(json.dumps(
        [event['kind'], event['created_at'], event['payload']],
        separators=(',', ':')).encode('utf-8')).hexdigest()


def _observed_history(rows, results, bounds):
    """Complete predecessor snapshots supersede legacy receipts, not live PIDs.

    Dropping unmarked receipts would hide later results; require the writer to
    have observed every other result/binding, independent of import order.
    """
    for event, payload in results:
        if (payload is None or payload.get('death_proof_version') != 1
                or 'result_role' in payload or 'outer_event_identity' in payload
                or type(payload.get('result_sequence')) is not int or payload['result_sequence'] <= 0):
            continue
        prior = payload.get('causal_predecessors')
        expected = [_event_identity(row) for row in rows if row['id'] != event['id']]
        if not isinstance(prior, list) or not all(isinstance(item, str) for item in prior):
            continue
        if sorted(prior) != sorted(expected):
            continue
        identity = payload.get('bound_event_identity')
        matching = [(row, bound) for row, bound in bounds
                    if bound is not None and _identity(row, bound) == identity]
        if len(matching) == 1:
            return matching[0], (event, payload), True
    return None


def result_identity_fields(conn, context):
    """Called under the result writer transaction, after its binding check."""
    params = (context['task_id'], context['run_id'])
    bound = conn.execute("SELECT * FROM task_events WHERE task_id=? AND run_id=? "
                         "AND kind='worker_bound' ORDER BY id DESC LIMIT 1", params).fetchone()
    if bound is None or (payload := _payload(bound)) is None:
        raise ValueError('result binding missing')
    from hermes_cli.kanban_lost_result import evidence_rows
    history = evidence_rows(conn, *params)
    sequences = [0]
    for event in history:
        prior = _payload(event)
        seq = prior.get('result_sequence') if prior else None
        if type(seq) is int and seq > 0:
            sequences.append(seq)
    return {'bound_event_identity': _identity(bound, payload), 'result_sequence': max(sequences) + 1,
            'causal_predecessors': [_event_identity(event) for event in history]}


def attributable_events(conn, task_id, run_id):
    """Choose modern evidence by writer sequence, never replica insertion order.

    Ambiguous sequences or mixed legacy/modern results fail closed. Rebindings
    with distinct identities also require ordinary operator recovery, not an
    inferred causal order from replica-local integers.
    """
    from hermes_cli.kanban_lost_result import evidence_rows
    rows = evidence_rows(conn, task_id, run_id)
    results = [(row, _payload(row)) for row in rows if row['kind'] != 'worker_bound']
    bounds = [(row, _payload(row)) for row in rows if row['kind'] == 'worker_bound']
    if not results:
        return None
    modern = any(p and p.get('death_proof_version') == 1 for _, p in results)
    if modern:
        owner = _owner_restart_events(conn, task_id, run_id, rows, results)
        if owner is not None:
            return owner
    if not bounds:
        return None
    if not modern:
        return bounds[-1], results[-1], False
    sequences = [p.get('result_sequence') if p else None for _, p in results]
    if any(type(seq) is not int or seq <= 0 for seq in sequences) or len(set(sequences)) != len(sequences):
        return _observed_history(rows, results, bounds)
    result = results[sequences.index(max(cast(list[int], sequences)))]
    assert result[1] is not None  # validated alongside its positive sequence above
    if result[0]['kind'] == 'worker_lost':
        return _observed_history(rows, results, bounds)
    if 'result_role' in result[1] or 'outer_event_identity' in result[1]:
        return None  # owner roles are accepted only through their witnessed path
    identity = result[1].get('bound_event_identity')
    if not isinstance(identity, dict):
        return None
    # Require every visible binding to agree; a later live rebind cannot be
    # hidden by importing its row before the earlier failed result.
    if any(payload is None or _identity(row, payload) != identity for row, payload in bounds):
        return _observed_history(rows, results, bounds)
    return bounds[-1], result, True
