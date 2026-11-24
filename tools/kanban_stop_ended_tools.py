"""Owning-chat stop-only control for an ENDED card on a local-executing shadow board. [daveey]

No ``expected_digest`` = read-only snapshot (or readback once stopped). With it = CAS apply, authorized only by the
live owner token the interactive turn runner binds (never by env, arguments or inherited routing context).
"""
import json
import sqlite3

from tools.kanban_containment_tools import _schema

_PROPERTIES = {
    'task_id': {'type': 'string'},
    'expected_digest': {'type': 'string', 'description': 'Omit to read the snapshot; pass its expected_digest to apply.'},
    'reason': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                 'items': {'type': 'string', 'minLength': 1, 'maxLength': 512}},
    'escalate_at': {'type': 'integer', 'description': 'Fixed future Unix deadline, at most 14 days out; never renewed.'}}
SCHEMA = _schema('kanban_stop_ended',
    'Owning chat: stop an ended shadow-board card without completing it. Leaves it nonexecuting in triage, '
    'with dependencies unsatisfied. Without expected_digest returns a read-only '
    'snapshot/readback. Receipt is LOCAL-SHADOW-ONLY (canonical_protected false); published to the hub only '
    'through the ordinary outbox once the Board advertises admin_stop_ingress. Never a release or completion.',
    _PROPERTIES, ['task_id'])


def _hub_ingress(board: str) -> int:
    import time
    from contextlib import closing
    from hermes_cli.kanban_sync_client import Hub, load_identity
    with closing(Hub(load_identity(), timeout=10, deadline=time.monotonic() + 30)) as hub:
        value = hub.status_origin_capability(board)
    flag = value.get('admin_stop_ingress') if isinstance(value, dict) else None
    return flag if type(flag) is int else 0


def handle(args, **kwargs):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_stop_context as ctx
    from hermes_cli import kanban_stop_ended as service
    try:
        with kt._board(args.get('board')) as (_, conn):
            from hermes_cli.kanban_db_sync import sync_state
            state = sync_state(conn) or {}
            board = state.get('board_id') or ''
            if not args.get('expected_digest'):
                return json.dumps(service.read(conn, board=board, task_id=args['task_id']))
            return json.dumps(service.apply(
                conn, board=board, task_id=args['task_id'], expected_digest=args['expected_digest'],
                owner=ctx.current(), capability=_hub_ingress, reason=args.get('reason'),
                evidence=args.get('evidence'), escalate_at=args.get('escalate_at')))
    except service.StopRefused as exc:
        return json.dumps({'ok': False, 'code': exc.code, 'error': str(exc), 'writes': 0,
                           'terminal_for_run': False, 'execution_authority': False})
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        return json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'terminal_for_run': False,
                           'execution_authority': False})
