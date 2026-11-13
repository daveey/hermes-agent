"""One Kanban card when the hub CPU advisory goes stale while the root CPU limit is on. [daveey]

With ``kanban.admission.cpu_busy_below_pct`` set, every claim on this host first reads the advisory sample
(``kanban_admission.hub_cpu_advisory``); a sample older than 10 s (``PRELEASE_MAX_AGE_MS``) defers the claim as
``hub_cpu_unknown``, so this alert calls a sample stale at that same age (the locked gate's looser 15 s would let a
sampler stuck 10-15 s behind defer every claim with no card). On
2026-11-12 every sample was dropped for four minutes and the hub claimed nothing with all slots free, and the only
signal was the dispatcher's "stuck" log line. Now the dispatcher calls :func:`check` once per tick: once the
advisory has been unusable for :data:`STALE_FOR_S` of this process's own observation (a gateway that just started
never files for the downtime before it), it opens ONE card (``idempotency_key='hub-cpu-advisory-stale'``) on the
default board, naming the sample age and this process's sampler counters. One incident files once; a fresh sample
ends it, and the next incident comments on the open card (at most hourly) or opens a new card once that card is done.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

KEY = "hub-cpu-advisory-stale"
AUTHOR = "hub-cpu-advisory"
STALE_FOR_S = 60.0
_COMMENT_EVERY = 3600


class _Incident:
    def __init__(self) -> None:
        self.stale_since: Optional[float] = None
        self.filed = False


_STATE = _Incident()


def _sample_age_s(now_wall: float) -> Optional[float]:
    """Seconds since the advisory sample's end, or None when it cannot be used (missing, malformed, future)."""
    from hermes_cli import machine_metrics as mm
    end = (mm.read_hub_cpu_sample() or {}).get("cpu_busy_sample_end_ms")
    if not isinstance(end, (int, float)) or isinstance(end, bool):
        return None
    age_ms = now_wall * 1000 - end
    return age_ms / 1000 if 0 <= age_ms else None


def check(*, mono: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
          state: Optional[_Incident] = None, file_fn: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """One dispatcher-tick observation. Returns the card id when this call filed (or commented) it. Never raises."""
    try:
        return _check(mono, wall, state or _STATE, file_fn or report)
    except Exception:  # health: allow BLE001 -- an alert must never break the dispatcher tick
        log.warning("kanban: hub CPU advisory staleness check failed", exc_info=True)
        return None


def _check(mono, wall, state: _Incident, file_fn) -> Optional[str]:
    from hermes_cli import kanban_admission as ka
    from hermes_cli import machine_cpu_metrics as cpu
    limit_state, _limit = cpu.root_limit()
    if limit_state != cpu.ON:
        state.stale_since, state.filed = None, False
        return None
    age = _sample_age_s(wall())
    if age is not None and age * 1000 <= ka.PRELEASE_MAX_AGE_MS:
        state.stale_since, state.filed = None, False  # fresh: the incident (if any) is over
        return None
    now = mono()
    if state.stale_since is None:
        state.stale_since = now
        return None
    if state.filed or now - state.stale_since < STALE_FOR_S:
        return None
    state.filed = True
    from hermes_cli import machine_cpu_sampler as sampler
    counters = ", ".join(f"{k}={v}" for k, v in sorted(sampler.status().items())) or "no sampler in this process"
    shown = "missing or unreadable" if age is None else f"{age:.0f} s old"
    return file_fn(f"advisory sample {shown}, unusable for {now - state.stale_since:.0f} s of dispatcher "
                   f"observation; sampler: {counters}")


def report(detail: str, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Open or comment the stale-advisory card on the default board. Best effort: never raises."""
    try:
        if conn is None:
            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_db_connect as kbc
            with kbc.connect_closing(kb.kanban_home() / "kanban.db") as own:
                return _report(own, detail)
        return _report(conn, detail)
    except Exception:  # health: allow BLE001 -- runs on the dispatcher tick
        log.warning("kanban: could not file the hub CPU advisory card (%s)", detail, exc_info=True)
        return None


def _report(conn: sqlite3.Connection, detail: str) -> str:
    from hermes_cli import kanban_db as kb
    log.warning("kanban: hub CPU advisory stale, every claim here defers hub_cpu_unknown: %s", detail)
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY status IN ('done', 'failed', 'cancelled'), created_at DESC, rowid DESC LIMIT 1", (KEY,),
    ).fetchone()
    if row and row[1] not in ("done", "failed", "cancelled"):
        recent = conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND author = ? AND created_at >= ? LIMIT 1",
            (row[0], AUTHOR, int(time.time()) - _COMMENT_EVERY),
        ).fetchone()
        if not recent:
            kb.add_comment(conn, row[0], AUTHOR, f"Again: {detail}")
        return row[0]
    body = (
        "The hub CPU advisory (`~/.hermes/cache/hub-cpu-sample.json`) is stale while the root "
        "`kanban.admission.cpu_busy_below_pct` is set, so every claim on this host defers as `hub_cpu_unknown` "
        f"and the host starts no cards.\n\n{detail}\n\n"
        "Check: the gateway's kanban sync loop runs (`hermes kanban sync status`), and agent.log for "
        "`machine metrics admission quality: cpu_sample_drops=` lines (each is a dropped advisory sample; the "
        "sampler is `hermes_cli/machine_cpu_sampler.py`). Done when the file's mtime advances every ~5 s and a "
        "`claimed` event follows on this host."
    )
    tid = kb.create_task(
        conn, title="Hub CPU advisory stale: every claim here defers hub_cpu_unknown", body=body,
        assignee="default", created_by=AUTHOR, priority=50, idempotency_key=None if row else KEY,
    )
    if row:  # the previous card is finished: the key now names this one (newest wins)
        with kb.write_txn(conn, allow_nested=True):
            conn.execute("UPDATE tasks SET idempotency_key = ? WHERE id = ?", (KEY, tid))
    return tid
