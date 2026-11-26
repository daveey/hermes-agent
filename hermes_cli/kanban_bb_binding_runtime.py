"""Inert canonical BB eligibility producer; no decision or write authority. [daveey]

Private module CLI: ``python -m hermes_cli.kanban_bb_binding_runtime REQUEST_ID``.
Only an independently read authenticated courier request selects its canonical
route. No courier action, native consumer or Factory response is registered here.
A successful snapshot is not enrolled-owner evidence and may already be stale.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing

from hermes_cli.kanban_bb_owner_binding import binding
from hermes_cli.kanban_card_answer import AnswerRefused
from hermes_cli.kanban_card_answer_runtime import _board_db, _profile
from hermes_cli.kanban_card_answer_transport import Records
from hermes_cli.kanban_lockguard import connect


def produce(request_id: str) -> dict:
    """Read eligibility only, never create/migrate a board or accept an envelope."""
    records = Records()
    profile, _ = _profile()
    record = records.read(request_id)
    if (not isinstance(record, dict) or record.get('id') != request_id
            or record.get('to_gw') != records.gateway or record.get('coll') != 'tasks'
            or record.get('action') != 'bb-owner-binding' or record.get('args') != {}
            or not isinstance(record.get('key'), str)):
        raise AnswerRefused('bb_binding_request_invalid')
    route = record['key'].split('/')
    if len(route) != 3 or route[0] != profile:
        raise AnswerRefused('bb_binding_request_invalid')
    _, board, task = route
    path = _board_db(board)
    with closing(connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
        result = binding(conn, task, owner_gateway=records.gateway, owner_profile=profile, board=board)
    # The complete raw ask contributes to generation but is never transported.
    return {key: result[key] for key in ('version', 'owner_gateway', 'owner_profile', 'board',
                                        'task', 'tenant', 'generation', 'choices')}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(json.dumps({'ok': False, 'error': 'usage'}))
        return 64
    try:
        out, code = {'ok': True, 'result': produce(argv[0])}, 0
    except AnswerRefused as exc:
        out, code = {'ok': False, 'refused': str(exc) or 'refused'}, 2
    except (OSError, sqlite3.OperationalError):
        out, code = {'ok': False, 'retryable': True, 'error': 'unavailable'}, 3
    except Exception as exc:  # health: allow BLE001 -- private module protocol hides unanticipated error contents
        out, code = {'ok': False, 'error': 'internal_error', 'type': type(exc).__name__}, 1
    print(json.dumps(out, sort_keys=True, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
