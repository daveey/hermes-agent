"""Historical local-store attestation, including evicted foreign task copies."""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_run_models import evidence, local_gateway


def attest(conn, task_id):
    """Hydrate history without authoring it; capture only our new attestation."""
    if not conn.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
        from hermes_cli.kanban_worker_node import import_cards, open_replica
        replica = open_replica(conn)
        if replica is not None:
            try:
                import_cards(conn, replica, [task_id])
            finally:
                replica.close()
    results = []
    with kb.write_txn(conn):
        gateway = local_gateway(conn)
        runs = conn.execute('SELECT id, uid FROM task_runs WHERE task_id=?', (task_id,)).fetchall()
        for run_id, uid in runs:
            receipt = evidence(conn, task_id, run_id)
            status = 'unknown_or_foreign'
            if receipt:
                prior = conn.execute("SELECT payload, origin_gateway FROM task_events "
                                     "WHERE task_id=? AND run_id=? AND kind='run_models_attested'",
                                     (task_id, run_id)).fetchall()
                exists = any(origin in (None, gateway) and json.loads(raw or '{}') == receipt
                             for raw, origin in prior)
                status = 'unchanged' if exists else 'conflicting_attestation' if prior else 'attested'
                if not prior:
                    kb._append_event(conn, task_id, 'run_models_attested', receipt, run_id=run_id)
            results.append({'task_id': task_id, 'run_uid': uid, 'status': status,
                            **(receipt or {})})
    return results or [{'task_id': task_id, 'status': 'missing_history'}]


def command(args):
    with kbc.connect() as conn:
        rows = [row for task_id in args.task_ids for row in attest(conn, task_id)]
    if args.json:
        print(json.dumps(rows, sort_keys=True))
    else:
        for row in rows:
            print(f"{row['task_id']} {row.get('run_uid', '-')} {row['status']} "
                  + ', '.join(row.get('session_models', [])))
    return 0
