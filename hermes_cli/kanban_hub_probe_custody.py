"""Keep probe custody separate from the execution placement of a ready card."""
from __future__ import annotations

import sqlite3


def registered_here(conn: sqlite3.Connection, task_id: str, probe: str) -> bool:
    """The latest park registers this exact probe on this gateway.

    Wake checks are machine-local and do not move with execution. Unknown legacy
    custody stays conservative; a later park must never borrow an older host.
    """
    from hermes_cli import kanban_continuation as kc
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    marks = ",".join("?" for _ in kc.SCHEDULED_ENTRY_EVENTS)
    row = conn.execute(
        f"SELECT payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (task_id, *kc.SCHEDULED_ENTRY_EVENTS)).fetchone()
    if row is None:
        return False
    payload = kb._json_dict(row[0])
    own = kwc.own_gateway(conn)
    return bool(own and payload.get("probe_host") == own and payload.get("wake_check") == probe)
