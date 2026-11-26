"""Bounded historical diagnostic carrier on existing authenticated Board events.

Not a lease/admission decision. The existing skip episode budget/dedupe is kept:
a once-recorded refusal expires; its remote reader then honestly reports unknown.
No reader clock or owner collector heartbeat renews the decision clock.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from hermes_cli.execution_observations import epoch, ident
from hermes_cli.kanban_nonrunning_reason import _unknown_reason, describe


_UNSET = object()


def binding(conn, row, *, causal_uid=_UNSET):
    """Portable evaluated task policy + causal UID, never local numeric IDs."""
    from hermes_cli import kanban_db_sync as sync
    policy = {k: row.get(k) for k in sync.SYNCED_COLUMNS['tasks'] if k != 'current_run_id'}
    # No current run is allowed on this diagnostic path; do not normalize an ID.
    if row.get('current_run_id') is not None:
        return None
    policy['created_on'] = row.get('created_on')
    if causal_uid is _UNSET:
        event = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind!='dispatch_skipped' "
                             "ORDER BY id DESC LIMIT 1", (row['id'],)).fetchone()
        causal_uid = event[0] if event else None
    if not ident(causal_uid):
        return None
    return {'policy_sha256': hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'causal_uid': causal_uid}


def envelope(conn, result, tid, *, captured, machine):
    from hermes_cli.kanban_capacity_evidence import _board, _qualified_reason
    if not isinstance(captured, dict) or not ident(machine):
        return None
    at = captured.get('observed_at')
    token = captured.get('tasks', {}).get(tid)
    if not epoch(at) or not isinstance(token, dict):
        return None
    row = token.get('row')
    reason = _qualified_reason(conn, result, tid, captured)
    if not isinstance(row, dict) or row.get('status') != 'ready' or not reason:
        return None
    try:
        bound = binding(conn, row)
    except (sqlite3.Error, TypeError, ValueError):
        return None  # old/unsynced schema cannot attest a portable lifecycle
    if bound is None:
        return None
    return {'version': 1, 'observer': machine, 'board': _board(conn),
            'task_id': tid, 'binding': bound, 'observed_at': at, 'valid_until': at + 180}


def read(conn, ids, *, board, now):
    from hermes_cli.kanban_execution_source import transaction
    with transaction(conn):
        return _read(conn, ids, board=board, now=now)


def replica_rows(conn, rows, *, now):
    """Acquire on the caller's existing replica snapshot, never mutate memo rows.

    Only a fresh matching Board row may supply a diagnosis. Owner profile is
    still resolved by the existing ledger/name path after this acquisition.
    """
    from hermes_cli.kanban_capacity_evidence import _board
    ids = list(dict.fromkeys(r['id'] for r in rows if r.get('status') == 'ready'))
    if not ids:
        return rows
    current = {}
    try:
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ','.join('?' for _ in chunk)
            current.update({r['id']: dict(r) for r in conn.execute(
                f'SELECT id,status,created_on,assignee FROM tasks WHERE id IN ({marks})', chunk)})
        reasons = _read(conn, ids, board=_board(conn), now=now)
    except (sqlite3.Error, TypeError, ValueError):
        return rows
    return [{**r, '_non_running_reason': reasons[r['id']]}
            if r['id'] in reasons and r['id'] in current
            and all(r.get(k) == current[r['id']].get(k) for k in ('status', 'created_on', 'assignee'))
            else r for r in rows]


def replica_fields(row, source, *, qualified, now):
    """Bind an acquired Board diagnosis to the reader's independently named owner."""
    if (not qualified or row.get('stale') or row.get('status') != 'ready'
            or not isinstance(source.get('_non_running_reason'), dict)):
        return {}
    from hermes_cli.kanban_execution_observations import card_raw
    from hermes_cli.execution_observations import nonrunning_reason
    raw = card_raw(row, now=now)
    raw['non_running_reason'] = nonrunning_reason(source['_non_running_reason'], now=now)
    return {'execution_observations': [raw]}


def _read(conn, ids, *, board, now):
    """Batched acquisition from feed-stamped origin_gateway, never payload authority.

    Local records with no authenticated feed origin are not remote evidence.
    The local machine snapshot remains the local path. An imported skip whose
    sender differs from its observer is rejected (including relayed history).
    """
    ids = list(dict.fromkeys(ids))
    unknown = _unknown_reason('dispatcher_evidence_missing')
    out = {tid: dict(unknown) for tid in ids}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ','.join('?' for _ in chunk)
        try:
            tasks = {r['id']: dict(r) for r in conn.execute(f'SELECT * FROM tasks WHERE id IN ({marks})', chunk)}
            events = conn.execute(
                f"SELECT e.task_id,e.uid,e.origin_gateway,e.payload,e.created_at FROM tasks t "
                f"JOIN task_events e ON e.id IN (SELECT id FROM task_events "
                f"WHERE kind='dispatch_skipped' AND task_id=t.id ORDER BY id DESC LIMIT 17) "
                f"WHERE t.id IN ({marks})", chunk)
            candidates = {}
            corrupt = set()
            counts = {}
            # Indexed newest causal identity; skip history never renews it.
            causal = {r['task_id']: r['uid'] for r in conn.execute(
                f"SELECT t.id AS task_id,(SELECT uid FROM task_events "
                f"WHERE kind!='dispatch_skipped' AND task_id=t.id ORDER BY id DESC LIMIT 1) AS uid "
                f"FROM tasks t WHERE t.id IN ({marks})", chunk)}
            bindings = {tid: binding(conn, row, causal_uid=causal.get(tid))
                        for tid, row in tasks.items() if row.get('status') == 'ready'}
            for event in events:
                tid = event['task_id']
                counts[tid] = counts.get(tid, 0) + 1
                if counts[tid] > 16:
                    corrupt.add(tid)  # omitted history may contain a newer decision clock
                    continue
                if tid not in bindings or not ident(event['origin_gateway']) or not ident(event['uid']):
                    continue
                raw = event['payload']
                if not isinstance(raw, str) or len(raw) > 32768:
                    continue
                try:
                    p = json.loads(raw)
                except ValueError:
                    # A bad optional record cannot hide unrelated cards or
                    # resurrect older evidence for this authenticated task.
                    corrupt.add(tid)
                    continue
                e = p.get('non_running_v1') if isinstance(p, dict) else None
                if not isinstance(e, dict) or e.get('version') != 1:
                    continue
                if (e.get('task_id') != tid or e.get('board') != board or e.get('observer') != event['origin_gateway']
                        or not bindings[tid] or e.get('binding') != bindings[tid]):
                    continue
                at, until = e.get('observed_at'), e.get('valid_until')
                # Keep a qualified newer unknown/expired decision as a tombstone;
                # filtering it first would resurrect an older blocked diagnosis.
                clock = at if epoch(at) else event['created_at']
                if not epoch(clock):
                    continue
                reason = _unknown_reason('dispatcher_evidence_stale')
                if all(epoch(t) for t in (at, until, now)) and at <= now < min(until, at + 180):
                    reason = (describe(p.get('reason'), p.get('detail'))
                              if isinstance(p.get('reason'), str) and (p.get('detail') is None or isinstance(p.get('detail'), str))
                              else _unknown_reason('dispatcher_evidence_unclassified'))
                    if reason['disposition'] == 'unknown':
                        reason = _unknown_reason('dispatcher_evidence_unclassified')
                if reason['disposition'] != 'unknown':
                    # Portable publication describes this observing dispatcher,
                    # never the fleet's eligibility or execution authority.
                    reason.update(scope='observing_dispatcher', observer=event['origin_gateway'], freshness='current',
                                  summary='Observing dispatcher: ' + reason['summary'], observed_at=at, valid_until=min(until, at + 180),
                                  evidence_ref='event:' + event['uid'], source_revision=event['uid'])
                candidates.setdefault(tid, {}).setdefault(event['origin_gateway'], []).append((clock, reason))
            for tid, observers in candidates.items():
                if tid in corrupt:
                    continue
                values = []
                for history in observers.values():
                    newest = max(clock for clock, _ in history)
                    latest = [v for clock, v in history if clock == newest]
                    if len({(v['code'], v['disposition']) for v in latest}) > 1:
                        latest = [_unknown_reason('dispatcher_evidence_conflict')]
                    values.append(latest[0])
                if any(v['freshness'] != 'current' for v in values):
                    out[tid] = _unknown_reason('dispatcher_evidence_missing')
                elif len({(v['code'], v['disposition']) for v in values}) > 1:
                    out[tid] = _unknown_reason('dispatcher_evidence_conflict')
                else:
                    out[tid] = max(values, key=lambda r: r['observed_at'])
        except (sqlite3.Error, TypeError, ValueError):
            # Missing old schema, corrupt data, or unavailable authority is unknown.
            continue
    return out
