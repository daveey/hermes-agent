"""Read-only dependency wait episodes for todo cards. No task-age or polling clocks."""
from __future__ import annotations

import json
import sqlite3

from hermes_cli.kanban_actionability_creation import EPISODE_EXITS
from hermes_cli.kanban_actionability_selection import LANDINGS
from hermes_cli.kanban_wait_info import epoch_s


def _data(raw):
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def describe(park, park_kind, since):
    """Project only the dependency payload verified by the graph reader."""
    payload = park if isinstance(park, dict) else _data(park)
    parents = payload.get('parents') if park_kind == 'dependency_wait' else None
    if not isinstance(parents, list) or not parents or not all(isinstance(p, str) and p for p in parents):
        return None
    return {'wait_state': 'waiting', 'waiting_on': 'card ' + ', '.join(parents),
            'waiting_since': epoch_s(since), 'agent_owned': False,
            'agent_owned_certain': True, 'premise': None}


def episode(events, *, parents_now):
    """Replay semantic entries/edge changes only; repeated links and dependency parks are not new episodes.

    A created event seeds graph/status evidence but is never an elapsed-time fallback. A later explicit
    dependency entry dates the first wait. An exit clears the clock even if history then becomes incomplete.
    """
    parents, phase, since = set(), None, None
    for kind, raw, at in events:
        payload = _data(raw)
        if kind == 'created':
            recorded = payload.get('parents')
            parents = set(recorded) if isinstance(recorded, list) and all(isinstance(p, str) for p in recorded) else set()
            phase, since = payload.get('status'), None
        elif kind in ('linked', 'unlinked'):
            parent = payload.get('parent')
            if not isinstance(parent, str):
                since = None
                continue
            changed = (parent not in parents) if kind == 'linked' else (parent in parents)
            if kind == 'linked':
                parents.add(parent)
            else:
                parents.discard(parent)
            if changed and phase == 'todo':
                since = epoch_s(at) if parents else None
        elif (kind == 'dependency_wait' or
              (kind in ('unblocked', 'status', *LANDINGS) and payload.get('status') == 'todo') or
              (kind == 'claim_rejected' and payload.get('reason') == 'parents_not_done')):
            if phase != 'todo' or since is None:
                since = epoch_s(at)
            phase = 'todo'
        elif kind in EPISODE_EXITS or kind in ('status', 'gave_up', 'block_loop_detected'):
            phase, since = payload.get('status'), None
    return since if phase == 'todo' and parents == set(parents_now) else None


def overlay(conn, parked, events):
    """Add todo rows only when a current parent actually gates them; unknown history stays undated.

    Use core's parent-state resolver so local parents and stored remote-parent mirrors follow readiness.
    This is presentation evidence, never permission to release/execute a card.
    """
    from hermes_cli.kanban_table_columns import table_columns
    from hermes_cli.kanban_db_parent_states import parent_blocks_many, parent_states_many
    from hermes_state_page_pack import read_rows

    ids = [tid for tid, row in parked.items() if row[0] == 'todo']
    if not ids:
        return
    try:  # One graph read for every todo card: this reader is on the sidebar poll path.
        graph = parent_states_many(conn, ids)
        blocks = parent_blocks_many(conn, [pair for tid in ids for pair in graph[tid]])  # bounded, not per parent
        gating = {tid: [pid for pid, _status in graph[tid] if blocks[pid]] for tid in ids}
    except sqlite3.Error:
        graph, gating = {}, {}  # No graph evidence, not proof of a dependency wait.
    for tid in ids:
        if not gating.get(tid):
            parked.pop(tid)
            events.pop(tid, None)
    ids = [tid for tid in ids if gating.get(tid)]
    if not ids:
        return
    history = {tid: [] for tid in ids}
    kinds = sorted(EPISODE_EXITS | set(LANDINGS) | {'created', 'linked', 'unlinked', 'dependency_wait',
                                                  'status', 'gave_up', 'block_loop_detected', 'claim_rejected'})
    marks = ','.join('?' for _ in kinds)
    try:
        columns = table_columns(conn, 'task_events')
        live = ' AND deleted_at IS NULL' if 'deleted_at' in columns else ''
        for tid, kind, raw, at in read_rows(conn,
                'SELECT task_id,kind,payload,created_at FROM task_events '
                f'WHERE task_id IN (SELECT value FROM json_each(?)) AND kind IN ({marks}){live} ORDER BY created_at,id',
                (json.dumps(ids), *kinds), 4):
            history[tid].append((kind, raw, at))
    except sqlite3.Error:
        pass  # Old/incomplete boards still name dependencies, without inventing a start time.
    for tid in ids:
        events[tid] = ('dependency_wait', {'parents': gating[tid]},
                       episode(history[tid], parents_now=[pid for pid, _status in graph[tid]]))
