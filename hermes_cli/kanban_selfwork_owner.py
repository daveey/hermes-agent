"""Authenticated courier-owner adapter; execution placement is not admission home.

The transport must reach a trusted courier receiver first. Public tool arguments
cannot supply intake context, and this adapter independently checks the installed
home tuple and physical database before committing an intake request.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from hermes_cli import kanban_selfwork as policy
from hermes_cli import kanban_selfwork_intake as intake


def _runtime():
    from hermes_cli.config import load_config_readonly
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_origin_adapter import _gateway
    from hermes_constants import get_hermes_home
    replica = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
    return load_config_readonly(), _gateway(replica), get_active_profile_name(), get_hermes_home().resolve()


def validate_declared_home(conn, value, intent, board):
    """Product creation keeps its existing route and does not load self-work policy."""
    if value is not None:
        validate_home(conn, intent, board)


def validate_home(conn, intent, board):
    from hermes_cli import kanban_db as kb
    if not isinstance(intent, dict) or not isinstance(intent.get('owner'), dict):
        raise policy.SelfWorkError('self-work requires authenticated routing to its configured intake home')
    if 'origin' not in intent and (
            not isinstance(intent.get('sender'), str)
            or not re.fullmatch(r'gw:[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', intent['sender'])):
        raise policy.SelfWorkError('self-work requires an authenticated gateway sender')
    config, gateway, profile, root = _runtime()
    kanban = config.get('kanban') if isinstance(config, dict) else None
    section = kanban.get('self_work') if isinstance(kanban, dict) else None
    home = section.get('home') if isinstance(section, dict) else None
    if (not isinstance(home, dict) or set(home) != {'gateway', 'profile', 'board'}
            or not all(isinstance(v, str) and v for v in home.values())):
        raise policy.SelfWorkError('self-work intake home must configure gateway, profile and board')
    if (home != intent['owner'] or gateway != home['gateway'] or profile != home['profile']
            or board != home['board'] or kb._normalize_board_slug(board) != board
            or not kb.board_exists(board) or kb.kanban_home().resolve() != root):
        raise policy.SelfWorkError('self-work intake home route mismatch')
    expected = root / 'kanban.db' if board == 'default' else root / 'kanban/boards' / board / 'kanban.db'
    actual = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if not actual or Path(actual).resolve() != expected or kb.kanban_db_path(board).resolve() != expected:
        raise policy.SelfWorkError('self-work intake home database mismatch')
    return gateway


def creator_task(conn, intent):
    """Return only an authoritative LOCAL lookup ID, never a foreign replica.

    The authenticated origin vouches for its foreign creator. Full provenance
    remains in the immutable intake intent, not in the local creator-ID field.
    """
    if 'creator' not in intent:
        return None
    creator = intent['creator']
    if (not isinstance(creator, dict)
            or set(creator) != {'gateway', 'profile', 'board', 'task_id', 'run_uid'}
            or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}', v)
                       for v in creator.values())):
        raise policy.SelfWorkError('self-work creator namespace unavailable at intake home')
    if {k: creator[k] for k in ('gateway', 'profile', 'board')} != intent['owner']:
        origin = intent.get('origin')
        if (not isinstance(origin, dict)
                or any(creator[k] != origin.get(k) for k in ('gateway', 'profile'))
                or intent.get('sender') != 'gw:' + creator['gateway']):
            raise policy.SelfWorkError('self-work creator namespace unavailable at intake home')
        return None  # Do not read a colliding local task, its tenant or subscriptions.
    columns = {row[1] for row in conn.execute('PRAGMA table_info(tasks)')}
    provenance = 'created_on' if 'created_on' in columns else 'NULL'
    row = conn.execute(f'SELECT {provenance} FROM tasks WHERE id=?', (creator['task_id'],)).fetchone()
    if row is None or row[0] not in (None, creator['gateway']):
        raise policy.SelfWorkError('self-work creator missing or foreign at intake home')
    return creator['task_id']


def snapshot(args):
    return json.loads(json.dumps(args)) if args.get('self_work') is not None else None


def replay(conn, args, intent, *, origin_intent=None):
    """Revalidate authority even for a replay; never rerun create preflight."""
    if args.get('self_work') is None:
        return None
    authority = validate_home(conn, intent, args.get('board'))
    request = {'args': args, 'intent': intent}
    receipt = intake.replay(conn, intent['request_id'], request)
    if receipt is None:
        from tools.kanban_tools import _persisted_identity, _coerce_str_list
        options = {key: args[key] for key in ('title', 'body', 'self_work', 'tenant', 'board') if key in args}
        options.update(origin_intent=origin_intent or (intent if 'origin' in intent else None),
                       creator_task_id=creator_task(conn, intent),
                       created_by=intent.get('sender') or _persisted_identity(),
                       parents=_coerce_str_list(args.get('parents') or [], 'parents', 'task ids'))
        from hermes_cli import kanban_selfwork_guards as guards
        try:
            receipt = intake.append_existing(conn, request_id=intent['request_id'], authority=authority,
                                             options=options, request=request, now=int(time.time()))
        except guards.GuardFrozen as exc:
            return guards.refusal(exc)
    return _render(conn, receipt, intent) if receipt is not None else None


def _render(conn, receipt, intent, marker_placement=None, place_origin=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_origin_creation
    from tools import kanban_tools as kt
    extra = {'handoff_complete': False, 'subscribed': False, **receipt}
    if receipt['task_id'] is None:
        return json.dumps({'ok': False, 'code': 'self_work_deferred', **extra})
    task = kb.get_task(conn, receipt['task_id'])
    if intent.get('origin') is not None:
        extra.update(kanban_origin_creation.origin_link_fields(conn, task.id, intent['origin']))
    if receipt['created'] and not receipt.get('replayed'):
        extra.update(kt._marker_placement_fields(conn, task.id, marker_placement, **(place_origin or {})))
    no_chat = intent.get('origin') is None
    extra['handoff_complete'] = no_chat
    return json.dumps({'ok': no_chat, 'code': 'self_work_accepted' if no_chat else 'origin_bind_pending',
                       **kt._fields(task, kt._ca.CREATED_FIELDS), **extra})


def create(conn, options, *, after_create, marker_placement, place_origin, request, intent=None):
    """Called by the existing tool path after normalization/preflight, before INSERT.

    Only DB-local effects execute under intake's transaction. Courier origins
    are subscribed by the origin ledger; no ambient session subscription or
    capability-routing network call is made here.
    """
    intent = intent if intent is not None else options.get('origin_intent')
    authority = validate_home(conn, intent, options.get('board'))
    options = dict(options)
    options['creator_task_id'] = creator_task(conn, intent)
    guard = options.pop('insert_guard', None)
    # Gateway forwarding would split one area across admission journals. Board
    # execution labels remain valid, but legacy deferred forwarding cannot.
    if marker_placement and marker_placement.get('defer'):
        raise policy.SelfWorkError('self-work admission stays at home; use Board execution labels, not gateway forwarding')
    from hermes_cli import kanban_selfwork_guards as guards
    try:
        receipt = intake.admit(conn, request_id=intent['request_id'], authority=authority,
                               local_gateway=authority, options=options, now=int(time.time()),
                               create_guard=guard, on_created=after_create,
                               request={'args': request, 'intent': intent})
    except guards.GuardFrozen as exc:
        return guards.refusal(exc)
    return _render(conn, receipt, intent, marker_placement, place_origin)
