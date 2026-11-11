"""Report a canonical state.db latch through independent Kanban storage, never Inbox."""
from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def schedule_incident(db_path, first_error: str) -> None:
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_db import kanban_db_path
    from hermes_cli.profiles import get_active_profile_name

    home = get_hermes_home()
    if Path(db_path).resolve() != (home / "state.db").resolve():
        return
    # Capture scope before starting a thread: profile context does not propagate to it.
    args = (home, first_error, kanban_db_path(), get_active_profile_name())
    try:
        threading.Thread(target=_report_safely, args=args, name="state-db-incident", daemon=True).start()
    except RuntimeError:
        logger.exception("state.db corruption incident reporter could not start")


def _report_safely(home: Path, first_error: str, board_path: Path, profile: str) -> None:
    try:
        report_incident(home, first_error, board_path=board_path, profile=profile)
    except Exception:  # health: allow BLE001 -- diagnostics must never replace the original storage failure
        logger.exception("state.db corruption incident reporting failed")


def _create_card(conn, path, first_error, profile, key):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_capabilities import CapabilityMismatchError

    options: dict[str, Any] = dict(
        title="Investigate state.db structural-corruption latch",
        body=(f"Canonical store: {path}\n\nFirst corrupt line:\n{first_error}\n\n"
              "The process has refused writes. Per-table read-only quick_check results "
              "will be appended by the reporter. Diagnose the first failure and check "
              "loaded code and other storage owners; do not recover or replace the live "
              "database without confirmed damage and a coordinated holder shutdown. "
              "Acceptance: identify and fix the cause, verify storage and writes, and "
              "record the diagnostic evidence. A later clean check does not prove the "
              "file was never damaged."),
        assignee=profile, created_by="state-health", priority=200, workspace_kind="scratch",
        requires=["terminal", "file", "git"], idempotency_key=key,
    )
    try:
        return kb.create_task(conn, **options)
    except CapabilityMismatchError:
        # Keep requirements for the specifier to route; do not drop the incident because
        # the observing profile cannot repair it. No user decision is requested.
        options.update(assignee=None, triage=True)
        return kb.create_task(conn, **options)


def report_incident(home: Path, first_error: str, *, board_path=None, profile=None) -> str | None:
    """One open card per canonical file; resolved/archived cards cannot own new incidents."""
    from gateway.status import _release_file_lock, _try_acquire_file_lock
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_comment_ids import add_comment
    from hermes_cli.kanban_db_connect import connect
    from hermes_cli.profiles import get_active_profile_name
    from hermes_state_corruption_confirm import confirm_corruption

    path = home / "state.db"
    board_path = board_path or kb.kanban_db_path()
    profile = profile or get_active_profile_name()
    fingerprint = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:24]
    prefix = f"state-corruption:{fingerprint}:"
    with (home / "state-corruption-report.lock").open("a+") as lock:
        if not _try_acquire_file_lock(lock):
            return None
        try:
            with closing(connect(board_path)) as conn:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE idempotency_key LIKE ? "
                    "AND status NOT IN ('done', 'archived') ORDER BY created_at DESC LIMIT 1",
                    (prefix + "%",),
                ).fetchone()
                if row:
                    add_comment(conn, row["id"], "state-health", f"Another storage owner latched: {first_error}")
                    return row["id"]
                task_id = _create_card(conn, path, first_error, profile, prefix + uuid.uuid4().hex)
            logger.warning("state.db corruption incident card: %s", task_id)
            result = confirm_corruption(path, frozenset(), None)
            rows = "\n".join(f"- {table}: {value}" for table, value in result.table_results)
            with closing(connect(board_path)) as conn:
                add_comment(conn, task_id, "state-health",
                            f"Read-only per-table quick_check: {result.describe()}\n"
                            f"{rows or 'No per-table results available; check failed before completion.'}")
            return task_id
        finally:
            _release_file_lock(lock)
