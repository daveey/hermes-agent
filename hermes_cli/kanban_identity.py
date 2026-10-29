"""Drop a finished Kanban run's identity before plugins load. [daveey]

A terminal app started from a worker (cmux via login -flp) keeps
``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID`` after that run ends. Every
hermes process and plugin then acts as that dead worker. Keep the pair only
when the run is still live on this machine's board. Live means a ``task_runs``
row for that id and task with ``ended_at`` NULL, holding a running claim: the
run is ``status='running'`` with a ``claim_lock``, and the task is
``status='running'`` with a ``claim_lock`` and ``current_run_id`` equal to
this run. If the board cannot be read, keep the pair — don't guess.

``HERMES_KANBAN_WORKER_TASK`` is a different marker (tool children). This
module does not touch it.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Mapping, MutableMapping, Optional

log = logging.getLogger("hermes_cli.kanban_identity")

TASK_KEY = "HERMES_KANBAN_TASK"
RUN_KEY = "HERMES_KANBAN_RUN_ID"

# Same predicate as ~/code/hermes home/bin/kanban_run_live.py (shims).
_LIVE_SQL = """
SELECT 1
FROM task_runs r
JOIN tasks t ON t.id = r.task_id AND t.current_run_id = r.id
WHERE r.id = ?
  AND r.task_id = ?
  AND r.ended_at IS NULL
  AND r.status = 'running'
  AND r.claim_lock IS NOT NULL
  AND t.status = 'running'
  AND t.claim_lock IS NOT NULL
LIMIT 1
"""


def _board_db(env: Mapping[str, str]) -> Optional[Path]:
    pinned = (env.get("HERMES_KANBAN_DB") or "").strip()
    if pinned:
        return Path(pinned).expanduser()
    try:
        from hermes_cli.kanban_db import kanban_db_path
        return kanban_db_path()
    except Exception:
        return None


def _connect(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise sqlite3.OperationalError(f"not a readable board: {db}")
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        conn.execute("select 1 from sqlite_master limit 1")
        return conn
    except sqlite3.OperationalError:
        conn = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=2)
        conn.execute("PRAGMA query_only=ON")
        return conn


def run_is_live(env: Mapping[str, str], task: str, run_id: int) -> Optional[bool]:
    """True when the run holds a live claim, False when it does not, None if the board can't be read."""
    db = _board_db(env)
    if db is None:
        return None
    try:
        conn = _connect(db)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(_LIVE_SQL, (int(run_id), task)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row is not None


def _emit(msg: str) -> None:
    log.info(msg)
    # A one-shot process often has only a queue handler, which has not flushed
    # by the time --version exits. stderr is the line the operator can see.
    print(msg, file=sys.stderr)


def drop_finished_kanban_identity(
    environ: Optional[MutableMapping[str, str]] = None,
) -> list[str]:
    """Remove ``HERMES_KANBAN_TASK`` and ``HERMES_KANBAN_RUN_ID`` unless that run is live.

    Returns the removed key names (empty when the pair is kept or was unset).
    Logs one info line when it drops them. An unreadable board keeps them.
    """
    env = os.environ if environ is None else environ
    task = (env.get(TASK_KEY) or "").strip()
    raw = (env.get(RUN_KEY) or "").strip()
    if not task and not raw:
        return []
    if task and raw.isdigit():
        live = run_is_live(env, task, int(raw))
        if live is None or live:
            return []
    removed = [key for key in (TASK_KEY, RUN_KEY) if key in env]
    for key in removed:
        env.pop(key, None)
    _emit(
        "dropped finished kanban identity "
        + " ".join(f"{key}={value}" for key, value in ((TASK_KEY, task), (RUN_KEY, raw)) if value)
        + " (run is not live)"
    )
    return removed
