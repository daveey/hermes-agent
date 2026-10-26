"""Local trusted-courier owner entry; creation is not origin acknowledgement.

The OS-local caller supplies the authenticated hub envelope. No network endpoint
or alternate direct-handler dispatch is provided here.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from hermes_cli.kanban_origin_adapter import _gateway


def receive(request: dict) -> dict:
    from hermes_cli.profiles import get_active_profile_name
    from tools import kanban_tools  # built-in self-registration
    from tools.registry import registry

    del kanban_tools
    profile = get_active_profile_name()
    replica = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
    gateway = _gateway(replica)
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import validate_binding
    if profile == 'custom':
        raise ValueError('origin_profile_unknown')
    if not isinstance(request, dict) or not isinstance(request.get('args'), dict):
        raise ValueError('origin_request_invalid')
    key = request.get('key')
    adopting = request.get('action') == 'origin-adopt'
    if not isinstance(key, str) or len(key.split('/')) != (3 if adopting else 2):
        raise ValueError('origin_route_mismatch')
    owner_profile, board = key.split('/')[:2]
    if (request.get('coll') != 'tasks' or request.get('action') not in {'create', 'origin-adopt'}
            or request.get('to_gw') != gateway or owner_profile != profile
            or kb._normalize_board_slug(board) != board
            or not kb.board_exists(board)):
        raise ValueError('origin_route_mismatch')
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    expected = home / 'kanban.db' if board == 'default' else home / 'kanban/boards' / board / 'kanban.db'
    if kb.kanban_home().resolve() != home or kb.kanban_db_path(board).resolve() != expected:
        raise ValueError('origin_route_mismatch')
    origin = request['args'].get('origin')
    validate_binding({'binding_id': 'validation', 'version': 1, 'publication_version': 1,
                      'owner': {'gateway': gateway, 'profile': profile, 'board': board, 'task_id': 'validation'},
                      'origin': origin, 'task_record_key': f'{profile}/{board}/validation'})
    if request.get('from') != f"gw:{origin['gateway']}":
        raise ValueError('origin_route_mismatch')
    intent = {'request_id': request['id'],
              'owner': {'gateway': gateway, 'profile': profile, 'board': board},
              'origin': request['args']['origin']}
    if adopting:
        if set(request['args']) != {'origin'}:
            raise ValueError('origin_request_invalid')
        task_id = key.split('/')[2]
        intent['owner']['task_id'] = task_id
        validate_binding({'binding_id': 'validation', 'version': 1, 'publication_version': 1,
                          'owner': intent['owner'], 'origin': origin, 'task_record_key': key})
        if not registry.get_definitions({'kanban_origin_adopt'}, quiet=True):
            return {'status': 'pending', 'code': 'kanban_tool_unavailable', 'handoff_complete': False}
        raw_result = registry.dispatch('kanban_origin_adopt', {'task_id': task_id, 'board': board},
                                       origin_intent=intent)
        return _dispatch_result(raw_result)
    create = request['args'].get('create')
    if (set(request['args']) != {'origin', 'create'} or not isinstance(create, dict)
            or not isinstance(create.get('session_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', create['session_id'])
            or ('board' in create and create['board'] != board)):
        raise ValueError('origin_request_invalid')
    args = {**create, 'board': board}
    if not registry.get_definitions({'kanban_create'}, quiet=True):
        return {'status': 'pending', 'code': 'kanban_tool_unavailable', 'handoff_complete': False}
    raw_result = registry.dispatch('kanban_create', args, origin_intent=intent)
    return _dispatch_result(raw_result)


def _dispatch_result(raw_result):
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else None
    except ValueError:
        result = None
    if not isinstance(result, dict) or result.get('handoff_complete') is not False:
        # Dispatch catches exceptions itself. An untyped error may follow a
        # committed write: keep the exact request retryable, never infer absence.
        return {'status': 'pending', 'code': 'origin_dispatch_pending', 'handoff_complete': False}
    if result.get('code') in {'origin_creation_conflict', 'origin_already_bound'}:
        # Replaying an immutable conflicting envelope cannot repair it. Reject
        # this request only; the original intent and its receipt remain intact.
        return {**result, 'status': 'rejected'}
    return result


def command(args) -> int:
    try:
        raw = sys.stdin.read() if args.request_file == '-' else Path(args.request_file).read_text(encoding="utf-8-sig")
        request = json.loads(raw)
        expected = {'origin-create': 'create', 'origin-adopt': 'origin-adopt'}.get(args.kanban_action)
        if not isinstance(request, dict) or expected is None or request.get('action') != expected:
            raise ValueError('origin_route_mismatch')
        result = receive(request)
    except (OSError, sqlite3.Error):
        result = {'status': 'pending', 'code': 'origin_storage_unavailable', 'handoff_complete': False}
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc)
        if not re.fullmatch(r'origin_[a-z_]+', code):
            code = 'origin_request_invalid'
        result = {'status': 'rejected', 'code': code, 'handoff_complete': False}
    print(json.dumps(result))
    return 0
