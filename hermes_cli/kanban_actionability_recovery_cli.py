"""Explicit diagnostic recovery CLI; frozen preview/digest confirmation. [daveey]"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import sys
import time

from hermes_cli import kanban_actionability_recovery as recovery

OBLIGATION = {'task_id', 'owner', 'action', 'instruction', 'prerequisite', 'trigger', 'evidence', 'deadline_at'}
CONFIRMATION = {'op_id', 'request_sha256'}


def _connection():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    path = kb.kanban_db_path()
    if not path.is_file():
        raise ValueError('existing primary home board required; no automatic initialization')
    return closing(connect(path))


def _hub():
    from hermes_cli.kanban_sync_client import Hub, load_identity
    return closing(Hub(load_identity(), timeout=10, deadline=time.monotonic() + 30))


def _request(args):
    if args.request_file == '-':
        raw = sys.stdin.read(65537)
    else:
        with open(args.request_file, encoding='utf-8-sig') as stream:
            raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('request exceeds 64K characters')
    data = json.loads(raw)
    fields = OBLIGATION if args.operation == 'preview' else CONFIRMATION
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError('request must contain exactly ' + ', '.join(sorted(fields)))
    return data


def command(args):
    """No lifecycle init/refresh, automatic deadline policy or implicit application."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable
    try:
        data = _request(args)
        with _connection() as conn, _hub() as hub:
            board = (sync.sync_state(conn) or {}).get('board_id')
            if args.operation == 'preview':
                task_id = data.pop('task_id')
                result = recovery.preview(conn, hub, board, task_id, **data)
            else:
                method = recovery.apply if args.operation == 'apply' else recovery.escalate
                result = method(conn, hub, board, data['op_id'], data['request_sha256'])
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, HubError, Resync, SyncUnavailable) as exc:
        print(f'kanban record-actionability: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0
