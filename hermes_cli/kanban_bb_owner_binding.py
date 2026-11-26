"""Read-only canonical ask binding for the BB native-owner adapter. [daveey]

This is target eligibility, not decision evidence or execution authority. No
transport or Factory consumer is registered until its producer is deployed.
"""
from __future__ import annotations

import json
import re
from uuid import UUID

from hermes_cli.kanban_card_answer import AnswerRefused, LIFECYCLE, digest
from hermes_cli.kanban_human_request import validate_human_request


def binding(conn, task: str, *, owner_gateway: str, owner_profile: str, board: str) -> dict:
    """Freeze one read snapshot; coordinates must come from the owning runtime.

    Own only a read transaction when the caller has none. An application caller
    must hold its writer reservation through this recheck, not an older read
    transaction. The raw ask is digest input only, never a client command surface.
    """
    route = (owner_gateway, owner_profile, board)
    if (any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}', v) for v in route)
            or not isinstance(task, str) or not re.fullmatch(r't_[a-zA-Z0-9_-]{1,100}', task)):
        raise AnswerRefused('bb_binding_route_invalid')
    own = not conn.in_transaction
    if own:
        conn.execute('BEGIN')
    try:
        return _read(conn, task, owner_gateway, owner_profile, board)
    finally:
        if own:
            conn.rollback()


def _read(conn, task, gateway, profile, board):
    required = {'kanban_sync_state': {'board_id', 'gateway_id', 'mode'},
                'tasks': {'id', 'tenant', 'created_on', 'status', 'human_request', 'block_kind'},
                'task_events': {'task_id', 'kind', 'uid'}}
    for table, columns in required.items():
        if not columns <= {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
            raise AnswerRefused('bb_binding_schema_unavailable')
    states = conn.execute('SELECT gateway_id,mode FROM kanban_sync_state WHERE board_id=?', (board,)).fetchall()
    if len(states) != 1 or tuple(states[0]) != (gateway, 'primary'):
        raise AnswerRefused('bb_binding_owner_unverified')
    row = conn.execute('SELECT tenant,created_on,status,human_request,block_kind FROM tasks WHERE id=?', (task,)).fetchone()
    if row is None or row[0] != 'babybear' or row[1] != gateway:
        raise AnswerRefused('bb_binding_target_unverified')
    if row[2] not in ('blocked', 'triage') or row[4] not in ('needs_input', 'capability'):
        raise AnswerRefused('bb_binding_ask_ineligible')
    try:
        ask = json.loads(row[3])
        checked = validate_human_request(ask, kind=row[4])
        if checked is None or ask.get('interaction') not in ('approval', 'reply'):
            raise ValueError('not a declared decision')
        choices = ask.get('choices')
        if not isinstance(choices, list) or any(set(c) != {'label', 'value'} for c in choices):
            raise ValueError('explicit exact choices required')
        # Validation strips whitespace. The canonical stored pairs, not the
        # normalized copies, are what the native client must display and send.
        if not choices or len({c['value'] for c in choices}) != len(choices):
            raise ValueError('ambiguous choices')
    except (ValueError, TypeError, RecursionError):
        raise AnswerRefused('bb_binding_ask_ineligible') from None
    marks = ','.join('?' for _ in LIFECYCLE)
    events = conn.execute(f'SELECT uid FROM task_events WHERE task_id=? AND kind IN ({marks})',
                          (task, *LIFECYCLE)).fetchall()
    identities = [r[0] for r in events]
    try:
        if not identities or any(not isinstance(v, str) or str(UUID(v)) != v for v in identities):
            raise ValueError('portable lifecycle required')
        if len(set(identities)) != len(identities):
            raise ValueError('duplicate lifecycle identity')
        generation = digest({'lifecycle': sorted(identities), 'ask': ask, 'status': row[2]})
    except (ValueError, TypeError, RecursionError):
        raise AnswerRefused('bb_binding_lifecycle_unverified') from None
    return dict(version=1, owner_gateway=gateway, owner_profile=profile, board=board, task=task,
                tenant='babybear', generation=generation, ask=ask, choices=choices)
