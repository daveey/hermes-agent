"""Authenticated same-gateway decision ownership, separate from courier adoption."""
from __future__ import annotations

import json
import os
import re

from hermes_cli.kanban_origin import OriginError


_ROUTING_CONTEXTS = ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_DELEGATED_CHILD_CONTEXT')


def adopt(conn, task_id: str) -> dict:
    """Only the current interactive chat may own an ownerless local card.

    CLI callers inherit the tool subprocess's session identity; no argument can
    nominate another chat. OS-local execution is the trust boundary, as for the
    courier CLI. Machine provenance wins over a copied interactive session ID.
    """
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_notify import _owning_sub_clause
    from hermes_cli.kanban_foreign_card import check as assert_local_card
    from hermes_cli.kanban_session_provenance import _read_sources, is_machine_session
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.profiles import get_active_profile_name

    sid = str(get_session_env('HERMES_SESSION_ID', '') or '')
    if (any(os.environ.get(key) or get_session_env(key, '') for key in _ROUTING_CONTEXTS)
            or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', sid)):
        raise OriginError('origin_interactive_required', 'local adoption requires the current interactive chat')
    # Fresh, positive proof, not the notification classifier's legacy unknown-ID allowance/cache.
    sources, readable = _read_sources(str(get_hermes_home() / 'state.db'), [sid])
    if not readable or sources.get(sid) not in {'desktop', 'cli', 'tui', 'acp', 'gui'}:
        raise OriginError('origin_interactive_required', 'local adoption requires local interactive session provenance')
    me, profile = gateway_names()[0], get_active_profile_name()
    with kb.write_txn(conn, allow_nested=True):
        assert_local_card(conn, task_id)
        task = kb.get_task(conn, task_id)
        if task is None:
            raise OriginError('origin_task_missing', 'exact local task does not exist')
        conflicts = []
        if task.session_id and task.session_id != sid and not is_machine_session(None, task.session_id):
            conflicts.append(task.session_id)
        for row in conn.execute(
                'SELECT s.platform,s.chat_id FROM kanban_notify_subs s JOIN tasks p ON p.id=s.task_id '
                f'WHERE s.task_id=? AND {_owning_sub_clause()}', (task_id,)):
            same = ((row['platform'] == 'tui' and row['chat_id'] == sid)
                    or (me and row['platform'] == 'hub' and row['chat_id'] == f'{me}/{profile}/{sid}'))
            if not same:
                conflicts.append(row['chat_id'])
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
            # An unreleased creator link also owns, even before its subscription is projected.

            for row in conn.execute("SELECT * FROM task_chat_links WHERE task_id=? AND released=0 AND kind='created'", (task_id,)):
                if (row['chat_gateway'], row['chat_profile'] or 'default', row['chat_session']) != (me, profile, sid):
                    conflicts.append(row['chat_session'])
        if conflicts:
            raise OriginError('origin_adoption_conflict', 'another chat already owns this card; local adoption never transfers ownership')
        # Preserve routing anchors and notification cursor when upgrading a shepherd row.
        row = conn.execute("SELECT delivery_metadata FROM kanban_notify_subs WHERE task_id=? AND platform='tui' "
                           "AND chat_id=? AND thread_id=''", (task_id, sid)).fetchone()
        try:
            metadata = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            raise OriginError('origin_adoption_conflict', 'existing subscription metadata is unreadable') from None
        if not isinstance(metadata, dict):
            raise OriginError('origin_adoption_conflict', 'existing subscription metadata is unreadable')
        metadata['reason'] = 'origin'
        conn.execute("INSERT INTO kanban_notify_subs "
                     "(task_id,platform,chat_id,thread_id,delivery_mode,delivery_metadata,created_at,last_event_id) "
                     "VALUES (?,'tui',?,'','notify',?,unixepoch(),"
                     "(SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?)) "
                     "ON CONFLICT(task_id,platform,chat_id,thread_id) DO UPDATE SET delivery_metadata=excluded.delivery_metadata",
                     (task_id, sid, json.dumps(metadata), task_id))
        if not row or json.loads(row[0] or '{}').get('reason') != 'origin':
            kb._append_event(conn, task_id, 'local_origin_adopted', {'session_id': sid})
    return {'ok': True, 'adopted': True, 'task_id': task_id, 'session_id': sid, 'ownership': 'decision'}


def command(args) -> int:
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_foreign_card import ForeignCardError
    from hermes_cli import kanban_db as kb
    try:
        if not args.task_id or args.request_file:
            raise OriginError('origin_request_invalid', 'use origin-adopt TASK --local without --request-file')
        board = getattr(args, 'board', None)
        if board and not kb.board_exists(board):
            raise OriginError('origin_task_missing', 'local adoption requires an existing board')
        with connect_closing(board=board) as conn:
            result = adopt(conn, args.task_id)
    except OriginError as exc:
        result = {'ok': False, 'code': exc.code, 'error': str(exc)}
    except ForeignCardError as exc:
        result = {'ok': False, 'code': 'foreign_card', 'error': str(exc)}
    print(json.dumps(result))
    return 0 if result['ok'] else 1
