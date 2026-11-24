"""Stop-only own-run lifecycle disposition, gated with the existing Kanban tools."""
import json

from tools.kanban_containment_tools import _schema

SCHEMA = _schema('kanban_retain',
    'End this admitted run in non-executing decision triage. '
    'Does not complete acceptance, renew spend or grant execution. '
    'No synthetic authority or human ask is required. '
    'Provide an administrative next step, evidence and a release_condition: the concrete thing the releaser should '
    'check, a note and never a gate. Except after a provider safety refusal or a containment hold, the release '
    "itself is `hermes kanban release <card>` on the card's home gateway by a human operator, the card's owning "
    "chat or a delegate chat (never this run); never name a permission, principal or grant that does not exist on "
    'this board as a precondition. Comments, timers and review completion alone do not release a stopped scope. '
    'After terminal_for_run=true stop. Unknown delivery preserves the exact request; do not replace it.',
    {'summary': {'type': 'string', 'minLength': 1,
                 'description': 'Bounded handoff summary, at most 2000 characters.'},
     'reason': {'type': 'string', 'enum': ['decision_pending', 'scope_stopped', 'capability_unavailable']},
     'next_step': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
     'release_condition': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
     'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 8,
                  'items': {'type': 'string', 'minLength': 1, 'maxLength': 512}},
     'needs': {'type': 'array', 'maxItems': 8, 'items': {'type': 'string'}}},
    ['summary', 'reason', 'next_step', 'release_condition', 'evidence'])


def handle(args, **kwargs):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_retain
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    from hermes_cli.kanban_sync_client import HubError
    try:
        with kt._board(args.get('board')) as (_, conn):
            from hermes_cli.kanban_db_sync import board_slug_for_path, sync_state
            state = sync_state(conn)
            board = state['board_id'] if state else board_slug_for_path(conn.execute('PRAGMA database_list').fetchone()[2])
            result = kanban_retain.retain(conn, board=board, **{k: v for k, v in args.items() if k != 'board'})
        return json.dumps(result)
    except (ValueError, ContainmentRefused, OSError, HubError) as exc:
        return json.dumps({'ok': False, 'terminal_for_run': False, 'error': str(exc),
                           'next_step': 'Preserve original command identity; owning deployment/decision route must resolve the stated prerequisite.'})
