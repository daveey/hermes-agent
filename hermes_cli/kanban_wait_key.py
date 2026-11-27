"""Resolve queued product keys using read-only Board observations."""
from __future__ import annotations

import time


def source_for_key(board, key, *, replica_only=False):
    from hermes_cli import kanban_wait_check as wait
    from hermes_cli.kanban_db_sync import board_slug_for_path
    from hermes_cli.kanban_sync_client import shadow_path
    from hermes_cli.kanban_replica_freshness import FRESH_SECONDS

    live = wait._db(board)
    shadow = shadow_path(board_slug_for_path(str(live)))
    paths = (shadow,) if replica_only else (live, shadow)
    found = []
    readable = False
    for path in paths:
        if not path.exists():
            continue
        # Absence and newest-card selection also depend on a current shadow.
        if path == shadow:
            state = wait._query(path, 'select pulled_at from kanban_sync_state limit 1')
            pulled = state[0][0] if state else None
            if not isinstance(pulled, (int, float)) or not 0 <= time.time() - pulled <= FRESH_SECONDS:
                raise ValueError('Board key replica is stale')
        rows = wait._query(path, 'select id,status,created_at from tasks where idempotency_key=? '
                                 'order by created_at desc,id desc limit 1', (key,))
        readable = True
        if rows:
            found.append((path, rows))
    if not readable:
        raise ValueError('no readable Board for key lookup')
    if not found:
        return live, []
    return max(found, key=lambda item: (item[1][0][2], item[1][0][0]))


def unreachable(gateway, key, *, board, until, since):
    """A restricted hub key can still read the same fresh Board shadow as task waits.

    Other gateway boards cannot be inferred from this gateway's Board shadow.
    A missing/stale/unreadable observation is cannot-tell, never pending.
    """
    from hermes_cli import kanban_wait_check as wait
    try:
        if wait.current_gateway(gateway) != 'hub':
            raise ValueError('remote key lookup unavailable')
        source, rows = source_for_key(board, key, replica_only=True)
        if not rows:
            return 1, 'queued card not found yet (board replica)'
        target, status = rows[0][:2]
        return wait._verdict(board, target, status, source=source, until=until, since=since,
                             note=' (board replica)')
    except Exception as exc:  # health: allow BLE001 -- failed observations are cannot-tell
        return 2, f'error: {exc}'
