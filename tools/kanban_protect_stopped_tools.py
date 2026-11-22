"""Registered stop-only administrative protection; no original workload admission."""
import json

from tools.kanban_containment_tools import _schema

_PROPERTIES = {
    'task_id': {'type': 'string'}, 'source_event_uid': {'type': 'string'}, 'source_run_uid': {'type': 'string'},
    'expected_status': {'type': 'string', 'enum': ['todo', 'ready', 'scheduled', 'triage', 'blocked', 'review']},
    'expected_current_run_uid': {'type': 'null'}, 'expected_updated_at': {'type': ['integer', 'null']},
    'expected_episode': {'type': ['string', 'null']}, 'requested_owner': {'type': 'string'},
    'next_step': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    'recovery_trigger': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    'escalate_at': {'type': 'integer', 'description': 'Fixed future Unix deadline, at most 14 days out; never renewed by retry.'},
    'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                 'items': {'type': 'string', 'minLength': 1, 'maxLength': 512}}}
SCHEMA = _schema('kanban_protect_stopped',
    'Request scoped home stop-only protection for an ended legacy target. '
    'Requires an admitted administrative requester that is its existing direct parent or exact decision owner. '
    'Preserves scope, history, dependencies and stronger restrictions; never grants execution or provider clearance. '
    'Synced calls report requested until exact canonical receipt and held replica confirm protection. '
    'Unknown delivery retains the same request, never fallback or replacement. Caller run remains active.',
    _PROPERTIES, list(_PROPERTIES))


def handle(args, **kwargs):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_protect_operator as service
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    from hermes_cli.kanban_supervisor_requests import SupervisorRequestError
    try:
        with kt._board(args.get('board')) as (_, conn):
            from hermes_cli.kanban_db_sync import sync_state, board_slug_for_path
            state = sync_state(conn)
            slug = state['board_id'] if state else board_slug_for_path(conn.execute('PRAGMA database_list').fetchone()[2])
            return json.dumps(service.request(conn, board=slug, **{k: v for k, v in args.items() if k != 'board'}))
    except (ValueError, ContainmentRefused, SupervisorRequestError, OSError) as exc:
        return json.dumps({'ok': False, 'terminal_for_run': False, 'execution_authority': False,
                           'error': str(exc), 'next_step': 'Preserve exact original request; resolve the stated canonical prerequisite.'})
