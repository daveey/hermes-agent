"""Read-only stranded-card report with exact streamed counts, no init/sync/network. [daveey]"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
import sqlite3
import sys
import time

from hermes_cli.kanban_actionability import NONTERMINAL
from hermes_cli.kanban_actionability_sources import Reader, describe
from hermes_cli.kanban_lockguard import connect


def report(conn: sqlite3.Connection, *, limit: int = 100, after: str = '', now: int | None = None) -> dict:
    """Counts cover the complete snapshot, pagination only limits displayed rows.

    Unknown and violated are independent: a partial replica may already demonstrate
    a violation without proving the absence of others. No zero-known-violation
    result is evidence of an invariant while unknown_count is nonzero.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError('limit must be between 1 and 1000')
    now = int(time.time()) if now is None else int(now)
    rows, count, violations, unknown, more = [], 0, 0, 0, False
    per_status = Counter()
    owned_transaction = not conn.in_transaction
    if owned_transaction:
        conn.execute('BEGIN')
    try:
        reader = Reader(conn)
        cursor = ''
        marks = ','.join('?' for _ in NONTERMINAL)
        while True:
            batch = conn.execute(f'SELECT id,status FROM tasks WHERE id>? AND status IN ({marks}) '
                                 'ORDER BY id LIMIT 128', (cursor, *NONTERMINAL)).fetchall()
            if not batch:
                break
            for task in batch:
                tid, status = task
                row = describe(conn, tid, now=now, reader=reader)
                count += 1
                per_status[status] += 1
                violations += bool(row['violations'])
                unknown += row['evidence_state'] == 'unknown'
                if tid > after:
                    if len(rows) < limit:
                        rows.append(row)
                    else:
                        more = True
            cursor = batch[-1][0]
    finally:
        if owned_transaction:
            conn.rollback()  # close this read snapshot, never the caller's transaction
    return {'version': 1, 'observed_at': now, 'total_nonterminal': count, 'scanned_count': count,
            'known_violation_count': violations, 'unknown_count': unknown, 'per_status': dict(per_status),
            'returned_count': len(rows), 'has_more': more,
            'next_after': rows[-1]['task_id'] if more and rows else None, 'rows': rows,
            'read_only': True, 'admission_authority': False}


def command(args) -> int:
    from hermes_cli import kanban_db as kb
    path = kb.kanban_db_path()
    try:
        with closing(connect(path.absolute().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            result = report(conn, limit=args.limit, after=args.after or '')
    except (sqlite3.Error, ValueError) as exc:
        print(f'kanban stranded: {exc}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"{result['total_nonterminal']} nonterminal; {result['known_violation_count']} with known violations; "
              f"{result['unknown_count']} with unknown evidence (not invariant success)")
        for row in result['rows']:
            blocker = row['blocker']
            step = blocker['trigger'] if blocker else row['instruction']
            if (row.get('release') or {}).get('authority') == 'kanban_release':
                step += f"; release: hermes kanban release {row['task_id']}"
            print(f"{row['task_id']} {row['status']} owner={row['owner']} {row['action']}: {step}; "
                  f"deadline={row['escalation']['at']} evidence={row['evidence_state']} "
                  f"violations={','.join(row['violations']) or 'none observed'}")
        if result['has_more']:
            print(f"More rows: --after {result['next_after']}")
    return 0
