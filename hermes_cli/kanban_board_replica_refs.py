"""Card-id lookups in this host's Board replicas (``kanban/sync-shadow/<board>.db``). [daveey]

A Board-synced host's live board file holds only part of each board (its own cards plus the copies it claimed); its
Board replica holds the whole board. So an id the live file lacks may still be a real card, and the phantom-ref
checks (the ``created_cards`` completion gate and the summary scan/diagnostics) ask the replica before the hub
replica, which can answer "fresh" yet lack a card minted minutes earlier.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional


def _replica_paths() -> list[Path]:
    from hermes_cli.kanban_db import kanban_home
    from hermes_cli.kanban_db_sync import SHADOW_DIR

    try:
        return sorted(p for p in (kanban_home() / "kanban" / SHADOW_DIR).glob("*.db") if p.is_file())
    except OSError:
        return []


def replica_tasks(ids: Iterable[str], parent: Optional[str] = None) -> tuple[dict[str, Optional[str]], set[str]]:
    """``({id: created_by}, linked)`` for the ``ids`` any Board replica holds; ``linked`` = those the replica links as
    children of ``parent``. An unreadable replica answers nothing (read-only; a missing file is never created)."""
    from hermes_cli.kanban_generation import open_ro

    want = list(dict.fromkeys(ids))
    found: dict[str, Optional[str]] = {}
    linked: set[str] = set()
    for path in _replica_paths() if want else ():  # nothing to look up: open no file (perf only)
        rest = [i for i in want if i not in found]
        marks = ",".join("?" * len(rest))
        try:
            conn = open_ro(path)
            try:
                found.update((r[0], r[1]) for r in conn.execute(
                    f"SELECT id, created_by FROM tasks WHERE id IN ({marks})", rest))
                linked.update(r[0] for r in conn.execute(
                    f"SELECT child_id FROM task_links WHERE parent_id = ? AND child_id IN ({marks})",
                    (parent, *rest)))
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return found, linked


def replica_prefixes(refs: Iterable[str]) -> set[str]:
    """The short-form ``refs`` (shorter than a full id) that prefix a card id in any Board replica."""
    from hermes_cli.kanban_generation import open_ro
    from hermes_cli.kanban_prose_refs import has_task_with_prefix, is_prefix_ref

    want = [r for r in dict.fromkeys(refs) if is_prefix_ref(r)]
    found: set[str] = set()
    for path in _replica_paths() if want else ():
        try:
            conn = open_ro(path)
            try:
                found.update(r for r in want if r not in found and has_task_with_prefix(conn, r))
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return found


def unresolved_after_replica(missing: list[str]) -> list[str]:
    """``missing`` (not on the local board) minus ids a Board replica or the hub replica knows (a short-form ref by
    prefix); [] when the hub replica cannot say for the rest."""
    known = set(replica_tasks(missing)[0]) | replica_prefixes(missing)
    missing = [p for p in missing if p not in known]
    try:
        from hermes_cli.kanban_session_activity import replica_task_resolution
        found, authoritative = replica_task_resolution(missing)
    except Exception:  # health: allow BLE001 -- advisory scan; an unusable replica means "unknown", never a flag
        return []
    if not authoritative:
        return []
    return [p for p in missing if p not in found]
