"""Comment-only courier for an interactive chat's accepted foreign origin binding. [daveey]

No replica write, unblock or human-answer authority. Identical text for the same
binding and owner ask reuses one durable courier ID, including after a timeout.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from hermes_cli.worker_drain_fence import fenced


def bound_owner(path: Path, *, task_id: str, board: str, gateway: str,
                profile: str, session: str, resolve_session) -> dict | None:
    """Read accepted, unreleased bindings; never infer authority from a replica."""
    from hermes_cli.kanban_origin_ledger import _binding_key, binding_hash, validate_binding
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    matches = {}
    # Origin-local ledger, not a Kanban board connection.
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:  # noqa: kanban-lockguard
        for key, digest, raw, task_raw in db.execute(
                'SELECT binding_id,binding_hash,binding_json,task_json FROM origin_obligations WHERE released=0'):
            binding = json.loads(raw)
            validate_binding(binding)
            if key != _binding_key(binding) or digest != binding_hash(binding):
                raise ValueError('origin_binding_invalid')
            owner, origin = binding['owner'], binding['origin']
            if (owner['task_id'] != task_id or owner['board'] != board
                    or owner['gateway'] == gateway or origin['gateway'] != gateway
                    or origin['profile'] != profile):
                continue
            if resolve_session(origin['session']) != session:
                continue
            task = json.loads(task_raw)
            ask = task.get('ask_notice') or {}
            route = {**owner, 'binding_hash': digest, 'ask_event': ask.get('event_id')}
            matches[json.dumps(owner, sort_keys=True)] = route
    if len(matches) > 1:
        raise ValueError('origin_comment_ambiguous')
    return next(iter(matches.values()), None)


@fenced('kanban.origin_comment')
def send(route: dict, *, session: str, body: str) -> dict:
    """Queue exactly once. Submission is not application; only owner outcomes confirm it."""
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli.kanban_placement import _sent_row
    if len(body) > 8000:
        raise ValueError('origin_comment_too_long')  # courier's legacy receiver truncates beyond this
    cli = reg.hub_cli_path()
    if not cli:
        raise ValueError('origin_comment_transport_unavailable')
    frozen = json.dumps([route, session, body], sort_keys=True, ensure_ascii=False)
    rid = 'origin-comment-' + hashlib.sha256(frozen.encode()).hexdigest()
    prior = _sent_row(rid)
    if not prior or prior[0] in ('local', 'queued'):
        env = dict(os.environ, HERMES_AGENT='1', HERMES_SESSION_ID=session)
        argv = [str(cli), 'request', route['gateway'], 'tasks',
                f"{route['profile']}/{route['board']}/{route['task_id']}", 'comment',
                '--arg', 'text=' + body, '--id', rid]
        try:
            subprocess.run(argv, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                           timeout=30, check=False, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            pass  # acceptance may be unknown: never replace the request ID
    row = _sent_row(rid)
    state = row[0] if row else 'unknown'
    result = {'task_id': route['task_id'], 'request_id': rid, 'forwarded_to': route['gateway']}
    if state == 'done':
        return {**result, 'status': 'comment_applied', 'message': 'Owner courier confirms comment application.'}
    if state in ('rejected', 'failed', 'expired'):
        return {**result, 'status': 'comment_unconfirmed', 'message':
                'Courier ended without confirmed application; inspect this exact request before retrying.'}
    return {**result, 'status': 'comment_pending', 'message':
            'Comment queued or outcome unknown; not yet confirmed on the owner. Retry identical text to inspect the same request.'}


def forward(conn, task_id: str, args: dict, body: str) -> dict | None:
    """Only interactive origin chats; workers retain their scoped operator route."""
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_origin_adapter import _gateway, _resolve_session
    if os.environ.get('HERMES_KANBAN_TASK') or conn.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
        return None
    session = get_session_env('HERMES_SESSION_ID', '')
    home = get_hermes_home()
    ledger = home / 'kanban/origin-ledger.db'
    if not session or not ledger.exists():
        return None
    replica = Path(os.environ.get('HERMES_HUB_STATE') or Path.home() / '.local/state/hermes-hub') / 'hub.db'
    route = bound_owner(ledger, task_id=task_id, board=args.get('board') or 'default',
                        gateway=_gateway(replica), profile=get_active_profile_name(),
                        session=_resolve_session(home / 'state.db', session),
                        resolve_session=lambda sid: _resolve_session(home / 'state.db', sid))
    if route is None:
        return None
    if args.get('relays_user_message') or args.get('relayed_human'):
        raise ValueError('origin_comment_human_relay_unsupported')
    return send(route, session=session, body=body)
