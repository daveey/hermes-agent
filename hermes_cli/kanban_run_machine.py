"""Which machine a listed card runs (or will run) on, for the Desktop linked-card strip. [daveey]

``run_machine`` is display-only: the Board decides placement. It reads the card's own columns:
- a running card, or one holding an unexpired lease (a Board assignment), runs on its ``lease_holder``;
- a waiting card pinned to one machine (``runs_on: ["machine:<m>"]``) runs on ``<m>``;
- a ``worker-first-v1`` card waits for the origin's worker pool (``POOL``);
- other ``runs_on`` labels (or an explicit ``[]``) name no single machine (None);
- otherwise the card runs where it was created (``created_on``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

POOL = "worker-pool"
_PLACEMENT = ("runs_on", "placement_policy", "lease_holder", "lease_expires")
_LOCAL = (*_PLACEMENT, "created_on")


def _labels(raw: Any) -> Optional[list[str]]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return [str(x) for x in raw] if isinstance(raw, list) else None


def _leased(row: dict[str, Any], now_ms: float) -> bool:
    try:
        return row.get("status") == "running" or float(row.get("lease_expires") or 0) > now_ms
    except (TypeError, ValueError):
        return False


def run_machine(row: dict[str, Any], now: Optional[float] = None) -> Optional[str]:
    """The machine name ``row`` runs on, ``POOL`` for a worker-pool card not yet leased, or None when unknown."""
    lease = str(row.get("lease_holder") or "")
    if lease and _leased(row, (time.time() if now is None else now) * 1000):
        return lease
    labels = _labels(row.get("runs_on"))
    machines = [x[len("machine:"):] for x in labels or [] if x.startswith("machine:")]
    if len(machines) == 1:
        return machines[0] or None
    if row.get("placement_policy") == "worker-first-v1" or "pool:worker" in (labels or []):
        return POOL  # [daveey] the worker queue (kanban_placement.POOL_LABEL): any worker with a free slot
    if labels is not None:
        return None
    return str(row.get("created_on") or "") or None


def placement_select(conn: sqlite3.Connection, alias: str = "", names: tuple[str, ...] = _PLACEMENT) -> str:
    """SELECT terms for ``names`` of ``tasks`` (aliased ``alias``); a column an older board lacks reads NULL."""
    from hermes_cli.kanban_table_columns import table_columns
    cols = table_columns(conn, "tasks")  # one result row, not one per column
    pre = f"{alias}." if alias else ""
    return ", ".join(f"{pre}{c}" if c in cols else f"NULL AS {c}" for c in names)


def add_run_machines(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Stamp ``run_machine`` on each listed card from its board row (display-only: a failed read stamps nothing)."""
    if not found:
        return
    ids = list(found)
    try:
        got = conn.execute(f"SELECT id, {placement_select(conn, names=_LOCAL)} FROM tasks "
                           f"WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
    except sqlite3.Error:
        return
    for r in got:
        card = found.get(r[0])
        if card is not None:
            row = dict(zip(_LOCAL, r[1:]))
            if machine := run_machine({**row, "status": card.get("status")}):
                card["run_machine"] = machine
