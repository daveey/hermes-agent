"""Fresh canonical evidence for an assignment repair whose original has foreign custody.

The Board's atomic execution inventory supplies the assignment AND its blocked
history. Replicas are not authority. Fetch before the writer lock; bind the
short-lived observation to the exact repair key and recheck local state inside
completion. This reads only: it never adopts, edits or releases the original.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import quote

from hermes_cli.kanban_assignment_routing import AssignmentRoutingError, _SUPERVISOR_PREFIX

_BUDGET_S = 10
_FIELDS = ('assignee', 'requires', 'workspace_kind', 'skills',
           'model_override', 'provider_override')


def repair_key(conn, task_id):
    row = conn.execute('SELECT idempotency_key FROM tasks WHERE id=?', (task_id,)).fetchone()
    key = row[0] if row else None
    return key if isinstance(key, str) and key.startswith(_SUPERVISOR_PREFIX) else None


@dataclass(frozen=True)
class OriginalObservation:
    repair: str
    key: str
    expires: float
    original: SimpleNamespace
    blocked_payloads: tuple[str, ...]


def _invalid():
    return AssignmentRoutingError('Assignment repair original has no complete canonical validation evidence')


def _list(value):
    if isinstance(value, str):
        value = json.loads(value)
    if value is not None and (not isinstance(value, list)
                              or any(not isinstance(v, str) or not v for v in value)):
        raise _invalid()
    return value


def _inventory(data, board, original, repair):
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1
            or data.get('board') != board or data.get('task_id') != original
            or data.get('events_complete') is not True or data.get('runs_complete') is not True
            or not isinstance(data.get('events'), list) or not isinstance(data.get('runs'), list)):
        raise _invalid()
    task = data.get('task')
    if (not isinstance(task, dict) or task.get('id') != original or task.get('uid') != original
            or task.get('deleted_at') is not None or any(k not in task for k in _FIELDS)
            or not isinstance(task['assignee'], str) or not task['assignee'].strip()
            or task['workspace_kind'] not in ('scratch', 'dir', 'worktree')):
        raise _invalid()
    fields = {k: task[k] for k in _FIELDS}
    # Workspace paths are host-local and omitted by the Board wire projection.
    # Scratch needs none; foreign dir/worktree validation must still fail closed.
    fields['workspace_path'] = task.get('workspace_path')
    for field in ('workspace_path', 'model_override', 'provider_override'):
        if fields[field] is not None and not isinstance(fields[field], str):
            raise _invalid()
    fields['requires'], fields['skills'] = _list(fields['requires']), _list(fields['skills'])
    if fields['workspace_kind'] != 'scratch' and not fields['workspace_path']:
        raise _invalid()
    payloads, seen, orders = [], set(), set()
    for event in data['events']:
        if (not isinstance(event, dict) or not isinstance(event.get('uid'), str) or not event['uid']
                or event['uid'] in seen or event.get('task_id') != original
                or event.get('deleted_at') is not None or type(event.get('ord')) is not int
                or event['ord'] in orders or not isinstance(event.get('kind'), str)):
            raise _invalid()
        seen.add(event['uid'])
        orders.add(event['ord'])
        if event['kind'] == 'blocked':
            raw = event.get('payload')
            value = json.loads(raw) if isinstance(raw, str) else None
            if not isinstance(value, dict):
                raise _invalid()
            if value.get('supervisor_task_id') == repair:
                if not isinstance(value.get('reason'), str):
                    raise _invalid()
                payloads.append((event['ord'], raw))
    # A complete history with no matching repair is not authority to release it.
    if not payloads:
        raise _invalid()
    return SimpleNamespace(id=original, **fields), tuple(raw for _, raw in sorted(payloads, reverse=True))


def prepare(conn, task_id):
    """One bounded authenticated read, only for missing local originals on synced boards."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_db_connect import _main_db_file

    key = repair_key(conn, task_id)
    if key is None or kb.get_task(conn, key.removeprefix(_SUPERVISOR_PREFIX)) is not None:
        return None
    if conn.in_transaction:
        raise _invalid()  # network under a writer transaction is never permitted
    path = _main_db_file(conn)
    config = sync.sync_config()
    if not path or not config.get('enabled') or not sync.board_selected(path, config):
        raise _invalid()
    board = sync.board_slug_for_path(path)
    original = key.removeprefix(_SUPERVISOR_PREFIX)
    expires = time.monotonic() + _BUDGET_S
    hub = None
    try:
        hub = client.Hub(client.load_identity(), timeout=_BUDGET_S, deadline=expires)
        data = hub._call('GET', f'/v2/boards/{quote(board, safe="")}/tasks/'
                         f'{quote(original, safe="")}/execution-evidence')
        task, payloads = _inventory(data, board, original, task_id)
        return OriginalObservation(task_id, key, expires, task, payloads)
    except (OSError, ValueError, TypeError, KeyError, client.HubError, client.Resync,
            client.SyncUnavailable) as exc:
        raise _invalid() from exc
    finally:
        if hub is not None:
            hub.close()


def resolve(conn, task_id, key, observation):
    """Transaction-local key check and bounded lifetime; never accept an old cached replica."""
    if (not isinstance(observation, OriginalObservation) or observation.repair != task_id
            or observation.key != key or time.monotonic() >= observation.expires):
        raise _invalid()
    return observation.original, observation.blocked_payloads
