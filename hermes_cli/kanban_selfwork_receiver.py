"""OS-local trusted courier intake. Envelope sender is authenticated by the caller.

Not a public JSON tool API: validated context travels only in registry kwargs.
Chat-bound envelopes retain the existing origin receiver and sender equality.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

from hermes_cli import kanban_selfwork_owner as owner

_ID = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}')


def receive(request: dict, *, exception=None) -> dict:
    """``exception`` is the home's private single-use capability (``kanban_selfwork_exception.reconsider``): a
    typed object in a keyword, never an envelope field. No CLI or tool entry of this module can supply it."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools  # built-in registration
    from tools.kanban_toolset_context import scoped_kanban_toolset_selection
    from tools.registry import registry

    del kanban_tools
    _, gateway, profile, root = owner._runtime()
    if not isinstance(request, dict) or not isinstance(request.get('args'), dict):
        raise ValueError('self_work_request_invalid')
    key, sender = request.get('key'), request.get('from')
    if (not isinstance(key, str) or len(key.split('/')) != 2
            or not isinstance(sender, str) or not sender.startswith('gw:')
            or not _ID.fullmatch(sender[3:])):
        raise ValueError('self_work_route_mismatch')
    owner_profile, board = key.split('/')
    if (request.get('action') != 'selfwork-create' or request.get('coll') != 'tasks'
            or request.get('to_gw') != gateway or profile == 'custom' or owner_profile != profile
            or kb._normalize_board_slug(board) != board or not kb.board_exists(board)):
        raise ValueError('self_work_route_mismatch')
    expected = root / 'kanban.db' if board == 'default' else root / 'kanban/boards' / board / 'kanban.db'
    if kb.kanban_home().resolve() != root or kb.kanban_db_path(board).resolve() != expected:
        raise ValueError('self_work_route_mismatch')
    body = request['args']
    create = body.get('create')
    if (not isinstance(request.get('id'), str) or not _ID.fullmatch(request['id'])
            or not isinstance(create, dict) or create.get('self_work') is None
            or set(body) not in ({'create'}, {'create', 'origin'}, {'create', 'origin', 'creator'})
            or ('board' in create and create['board'] != board)
            or any(k in create for k in ('origin_intent', 'selfwork_intent', 'origin_session', 'creator_task_id',
                                          'selfwork_exception'))):
        raise ValueError('self_work_request_invalid')
    if exception is not None:  # the exact authenticated original, hashed before the action is normalised below
        from hermes_cli import kanban_selfwork_exception as sx
        sx.bind(exception, request)
    if 'origin' in body:
        from hermes_cli.kanban_origin_owner import receive as receive_origin
        origin_body = {k: v for k, v in body.items() if k != 'creator'}
        context = {'creator': body['creator'], 'sender': sender} if 'creator' in body else None
        return receive_origin({**request, 'action': 'create', 'args': origin_body}, selfwork_context=context,
                              selfwork_exception=exception)
    if exception is not None or create.get('session_id') is not None:
        raise ValueError('self_work_request_invalid')
    intent = {'request_id': request['id'], 'sender': sender,
              'owner': {'gateway': gateway, 'profile': profile, 'board': board}}
    with scoped_kanban_toolset_selection(['kanban']):
        if not registry.get_definitions({'kanban_create'}, quiet=True):
            return {'status': 'pending', 'code': 'kanban_tool_unavailable', 'handoff_complete': False}
        raw = registry.dispatch('kanban_create', {**create, 'board': board}, selfwork_intent=intent)
    result = json.loads(raw) if isinstance(raw, str) else None
    if not isinstance(result, dict) or not isinstance(result.get('handoff_complete'), bool):
        return {'status': 'pending', 'code': 'self_work_dispatch_pending', 'handoff_complete': False}
    return result


def command(args) -> int:
    try:
        raw = sys.stdin.read() if args.request_file == '-' else Path(args.request_file).read_text(encoding='utf-8-sig')
        result = receive(json.loads(raw))
    except (OSError, sqlite3.Error):
        result = {'status': 'pending', 'code': 'self_work_storage_unavailable', 'handoff_complete': False}
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc)
        if not re.fullmatch(r'(self_work|origin)_[a-z_]+', code):
            code = 'self_work_request_invalid'
        result = {'status': 'rejected', 'code': code, 'handoff_complete': False}
    print(json.dumps(result))
    return 0
