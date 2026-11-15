"""Single-home self-work submission, independent of execution placement.

Queue acknowledgement is not task creation. No fallback to a local writer, even
when the configured home is this gateway. Foreign chat forwarding fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime

from hermes_cli.kanban_selfwork import SelfWorkError, contract

_ID = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}')


def prepare(args):
    from hermes_cli.kanban_selfwork_owner import _runtime
    from hermes_cli import kanban_origin
    from gateway.session_context import get_session_env

    contract(args.get('self_work'))
    config, gateway, profile, _ = _runtime()
    home = config.get('kanban', {}).get('self_work', {}).get('home')
    if (not isinstance(home, dict) or set(home) != {'gateway', 'profile', 'board'}
            or not all(isinstance(v, str) and _ID.fullmatch(v) for v in home.values())
            or not isinstance(gateway, str) or not _ID.fullmatch(gateway)):
        raise SelfWorkError('self-work intake home must configure gateway, profile and board')
    worker = bool(os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK'))
    creator = None
    if worker:
        from hermes_cli.kanban_selfwork_discovery import worker_creator
        creator = worker_creator()
    create = json.loads(json.dumps(args))
    # Freeze the sender's scope before hashing/queueing. The intake receiver must
    # never substitute its own ambient tenant for this immutable value.
    create['tenant'] = create.get('tenant') or os.environ.get('HERMES_TENANT')
    raw_origin = create.pop('origin_session', None) or kanban_origin.ambient_origin_ref()
    session = create.pop('session_id', None) or get_session_env('HERMES_SESSION_ID')
    origin = kanban_origin.parse_origin(raw_origin) if raw_origin else None
    if worker and origin is None:
        raise SelfWorkError('self-work worker forwarding requires a qualified origin session')
    if not worker and origin is not None and origin.gateway != gateway:
        raise SelfWorkError('self-work foreign origin must submit from its authenticated origin gateway')
    if origin is None and session:
        origin = kanban_origin.Origin(gateway, profile, session)
    create['board'] = home['board']
    create.setdefault('assignee', profile)
    body = {'create': create}
    if origin:
        if not all(isinstance(v, str) and _ID.fullmatch(v) for v in (origin.gateway, origin.profile, origin.session)):
            raise SelfWorkError('invalid self-work origin identity')
        if not worker:
            create['session_id'] = session or origin.session
        body['origin'] = {'gateway': origin.gateway, 'profile': origin.profile, 'session': origin.session}
    identity = {'sender': gateway, 'home': home, 'key': create.get('idempotency_key')}
    if worker:
        body['creator'] = creator
        identity.update(creator=creator, origin=body['origin'])
    if not identity['key']:
        identity['body'] = body
        identity['day'] = datetime.now(UTC).date().isoformat()
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return home, body, 'sw-' + digest


def submit(args):
    home, body, request_id = prepare(args)
    from tools.environments.local import build_subprocess_env
    if 'creator' in body:
        origin = body['origin']
        route = [origin['gateway'], 'sessions', origin['profile'] + '/' + origin['session'], 'selfwork-forward']
    else:
        route = [home['gateway'], 'tasks', home['profile'] + '/' + home['board'], 'selfwork-create']
    argv = ['hermes-hub', 'request', *route, '--id', request_id, '--args-file', '-']
    try:
        result = subprocess.run(argv, input=json.dumps(body), capture_output=True, text=True,  # windows-footgun: ok (encoding on next line)
                                encoding='utf-8', timeout=30, check=False, env=build_subprocess_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SelfWorkError('self-work transport unavailable; retry the same request, never create locally') from exc
    if result.returncode not in (0, 3) or request_id not in result.stdout.splitlines():
        raise SelfWorkError('self-work transport refused: ' + result.stderr.strip()[:1000])
    return {'ok': False, 'created': False, 'handoff_complete': False, 'code': 'self_work_queued',
            'request_id': request_id, 'home': home, 'queued': True}


def cli_submit(args, *, body, workspace_kind, workspace_path, max_runtime, branch_name):
    if branch_name or getattr(args, 'reasoning_effort', None):
        raise SelfWorkError('self-work courier does not support branch or reasoning_effort yet')
    names = ('title', 'assignee', 'tenant', 'priority', 'admission_class', 'admission_frees',
             'self_work', 'project', 'triage', 'idempotency_key', 'skills', 'max_retries',
             'pin_mandatory', 'goal_mode', 'review_required', 'goal_max_turns', 'completion_contract',
             'initial_status', 'human_request', 'runs_on', 'requires', 'distinct_from', 'distinct_reason',
             'origin_session')
    values = {key: getattr(args, key) for key in names if getattr(args, key, None) is not None}
    values.update(body=body, workspace_kind=workspace_kind, workspace_path=workspace_path,
                  parents=list(args.parent or ()), max_runtime_seconds=max_runtime)
    for old, new in (('model_override', 'model'), ('provider_override', 'provider')):
        if getattr(args, old, None) is not None:
            values[new] = getattr(args, old)
    from hermes_cli.kanban_cli_body import session_identity
    worker = bool(os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK'))
    _, session_id = session_identity(worker)
    if session_id is not None:
        values['session_id'] = session_id
    return submit(values)


def cli_command(args, **kwargs):
    from hermes_cli.kanban import _err, _print_json
    try:
        result = cli_submit(args, **kwargs)
    except (SelfWorkError, ValueError, OSError) as exc:
        return _err(f'kanban: {exc}', 2)
    _print_json(result)
    return 0
