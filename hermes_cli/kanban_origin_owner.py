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


def _dispatch_owner_tool(name, args, intent, selfwork_context=None):
    """The validated courier selects Kanban explicitly, independent of chat defaults.

    Keep registry availability checks (including delegated-child fences) and the
    real dispatch path; never mutate profile configuration or process env.
    """
    from tools.kanban_toolset_context import scoped_kanban_toolset_selection
    from tools.registry import registry

    with scoped_kanban_toolset_selection(['kanban']):
        if not registry.get_definitions({name}, quiet=True):
            return {'status': 'pending', 'code': 'kanban_tool_unavailable', 'handoff_complete': False}
        private = {'selfwork_intent': {**intent, **selfwork_context}} if selfwork_context else {}
        return _dispatch_result(registry.dispatch(name, args, origin_intent=intent, **private))


def receive(request: dict, *, selfwork_context=None) -> dict:
    from hermes_cli.profiles import get_active_profile_name
    from tools import kanban_tools  # built-in self-registration

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
        return _dispatch_owner_tool('kanban_origin_adopt', {'task_id': task_id, 'board': board}, intent)
    create = request['args'].get('create')
    if (set(request['args']) != {'origin', 'create'} or not isinstance(create, dict)
            or not isinstance(create.get('session_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', create['session_id'])
            or ('board' in create and create['board'] != board)):
        raise ValueError('origin_request_invalid')
    args = {**create, 'board': board}
    return _dispatch_owner_tool('kanban_create', args, intent, selfwork_context)


def _dispatch_result(raw_result):
    try:
        result = json.loads(raw_result) if isinstance(raw_result, str) else None
    except ValueError:
        result = None
    if (isinstance(result, dict) and result.get('created') is False
            and isinstance(result.get('code'), str) and result['code'].strip()):
        # Definitive no-write refusals cannot be repaired by replaying this envelope.
        return {**result, 'status': 'rejected', 'handoff_complete': False}
    if not isinstance(result, dict) or result.get('handoff_complete') is not False:
        # Dispatch catches exceptions itself. An untyped error may follow a
        # committed write: keep the exact request retryable, never infer absence.
        return {'status': 'pending', 'code': 'origin_dispatch_pending', 'handoff_complete': False}
    if result.get('code') in {'origin_creation_conflict', 'origin_already_bound', 'origin_idempotency_conflict'}:
        # Replaying an immutable conflicting envelope cannot repair it. Reject
        # this request only; the original intent and its receipt remain intact.
        return {**result, 'status': 'rejected'}
    return result


def snapshot_command(args) -> int:
    """Owner-side read for the origin guard: answer only for this exact gateway/profile route."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        from hermes_cli.kanban_origin_snapshot import owner_evidence
        raw = sys.stdin.read() if args.request_file == '-' else Path(args.request_file).read_text(encoding="utf-8-sig")
        request = json.loads(raw)
        replica = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
        gateway = _gateway(replica)
        body = request.get('args')
        owner = body.get('owner')
        if (request.get('action') != 'ownership-snapshot' or not isinstance(owner, dict)
                or set(body) != {'owner', 'binding_id'} or owner.get('gateway') != gateway
                or owner.get('profile') != get_active_profile_name() or request.get('to_gw') != gateway):
            raise ValueError('origin_route_mismatch')
        result = owner_evidence(owner, body['binding_id'])
    except (OSError, sqlite3.Error):
        result = {'schema': 1, 'state': 'unknown', 'reasons': ['origin_storage_unavailable']}
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        code = str(exc)
        result = {'schema': 1, 'state': 'unknown',
                  'reasons': [code if re.fullmatch(r'origin_[a-z_]+', code) else 'origin_request_invalid']}
    print(json.dumps(result))
    return 0


def command(args) -> int:
    if args.kanban_action == 'origin-adopt' and getattr(args, 'local', False):
        from hermes_cli.kanban_local_adoption import command as local_command
        return local_command(args)
    if args.kanban_action == 'selfwork-producer':
        from hermes_cli.kanban_selfwork_producer import command as producer_command
        return producer_command(args)
    if args.kanban_action == 'selfwork-forward':
        from hermes_cli.kanban_selfwork_forward import command as forward_command
        return forward_command(args)
    if args.kanban_action == 'selfwork-create':
        from hermes_cli.kanban_selfwork_receiver import command as intake_command
        return intake_command(args)
    if args.kanban_action == 'ownership-snapshot':
        return snapshot_command(args)
    try:
        if args.kanban_action == 'origin-adopt' and (getattr(args, 'task_id', None) or not args.request_file):
            raise ValueError('origin_request_invalid')
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
