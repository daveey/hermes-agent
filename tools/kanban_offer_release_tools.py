"""``kanban_offer_release``: ask the card's home to file its stamped Release / Don't ask. [daveey]

Releases nothing: only a person's recorded answer (or a human operator's ``hermes kanban release``) does
(``hermes_cli.kanban_release_offer``). Refused with the reason under a safety or containment hold.
"""
import json
import os
import sqlite3

from tools.kanban_containment_tools import _schema

SCHEMA = _schema('kanban_offer_release',
    "Show a working Release button on a stopped card only a person can release. Files the stamped "
    "Release / Don't ask for its pending stop on the card's home (e.g. an exhausted reviewer allowance), shown in "
    "every chat linked to it and in for_chat (default: this chat). Releases nothing itself; the person's "
    "answer does. Refused under a safety or containment hold.",
    {'task_id': {'type': 'string'},
     'for_chat': {'type': 'string', 'description': 'Requesting chat session; default this chat.'}},
    ['task_id'])


def handle(args, **kwargs):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_release_offer as offers
    chat = args.get('for_chat') or os.environ.get('HERMES_SESSION_ID') or None
    try:
        with kt._board(args.get('board')) as (_, conn):
            return json.dumps({'ok': True, **offers.offer_release(conn, args['task_id'], for_chat=chat),
                               'released': False})
    except offers.OfferRefused as exc:
        return json.dumps({'ok': False, 'outcome': 'refused', 'refusal': exc.code, 'error': str(exc),
                           'released': False})
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        return json.dumps({'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'released': False})
