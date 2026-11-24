"""Explicit cooperative home maintenance owner; no sweep or automatic activation. [daveey]"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_owner as owner
from hermes_cli.kanban_sync_client import Hub, load_identity


def command(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=('source', 'destination', 'reconcile-precommit'))
    parser.add_argument('--task', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', default='hub', choices=('hub',))
    parser.add_argument('--nonce', required=True)
    parser.add_argument('--expected-version', required=True, type=int)
    parser.add_argument('--board', default='default')
    parser.add_argument('--db', type=Path)
    args = parser.parse_args(argv)
    # This is administrative custody movement, never a worker-side lifecycle
    # alternative. Source/destination eligibility is still checked transactionally.
    if os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_KANBAN_WORKER_TASK'):
        raise PermissionError('cooperative home maintenance requires the owner operator surface')
    identity = {k: getattr(args, k.replace('-', '_')) for k in ('task', 'source', 'destination', 'nonce', 'expected_version')}
    methods = {'source': owner.source_step, 'destination': owner.destination_step,
               'reconcile-precommit': owner.reconcile_precommit}
    hub = Hub(load_identity())
    conn = dbc.connect(args.db) if args.db else dbc.connect()
    try:
        record = methods[args.step](conn, hub, args.board, identity)
        print(json.dumps({**identity, 'phase': record['phase'], 'row_count': len(record['rows'])}, sort_keys=True))
        return 0
    finally:
        conn.close()
        hub.close()


if __name__ == '__main__':
    raise SystemExit(command())
