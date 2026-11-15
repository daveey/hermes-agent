"""Task-id references in completion prose: the advisory ``suspected_hallucinated_references`` scan. [daveey]

A prose token shorter than a full id (``t_`` + 16 hex) is a short form: it resolves when any card id (local board,
Board replica or hub replica) starts with it, so a card's own ``t_01a1cf03`` prefix is not a phantom. A token inside a
path, branch or identifier (``design/t_01a1cf03-land-lane``) is not a reference at all.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Iterable

# 8+ hex: legacy ``_new_task_id`` (8), ``kanban_create`` (12) and current (16) ids, plus their prefixes. The
# lookbehind matches ``kanban_block_policy._TASK_ID``.
_TASK_ID_PROSE_RE = re.compile(r"(?<![\w/])t_[a-f0-9]{8,}\b")
FULL_TASK_ID_LEN = len("t_") + 16


def is_prefix_ref(ref: str) -> bool:
    return len(ref) < FULL_TASK_ID_LEN


def has_task_with_prefix(conn: sqlite3.Connection, prefix: str) -> bool:
    """A ``tasks`` row whose id starts with ``prefix`` (GLOB: ``_`` is literal, unlike LIKE)."""
    return conn.execute("SELECT 1 FROM tasks WHERE id GLOB ? LIMIT 1", (prefix + "*",)).fetchone() is not None


def hub_replica_lookup(ref: str, after_seq: int | None = None) -> tuple[str, tuple]:
    """``(sql, params)`` finding ``ref`` among the hub replica's task keys (``[profile/]board/<id>``): the key's last
    segment equals a full id, or starts with a short-form ref. ``after_seq`` narrows it to rows written after that
    feed ``seq`` (``kanban_board_replica_refs`` re-checks a known-absent ref against only the rows a pull added)."""
    newer, extra = ("", ()) if after_seq is None else (" AND seq > ?", (after_seq,))
    if is_prefix_ref(ref):
        return f"SELECT 1 FROM replica WHERE coll = 'tasks' AND key GLOB ?{newer} LIMIT 1", (f"*/{ref}*", *extra)
    return (f"SELECT 1 FROM replica WHERE coll = 'tasks' AND substr(key, -?) = ?{newer} LIMIT 1",
            (len(ref) + 1, "/" + ref, *extra))


def _missing_task_refs(conn: sqlite3.Connection, refs: Iterable[str]) -> list[str]:
    """Refs (order kept) with no local task: full ids by exact id, short ones by prefix."""
    from hermes_cli.kanban_db import _missing_task_ids

    refs = list(refs)
    missing = set(_missing_task_ids(conn, [r for r in refs if not is_prefix_ref(r)]))
    missing.update(r for r in refs if is_prefix_ref(r) and not has_task_with_prefix(conn, r))
    return [r for r in refs if r in missing]


def _scan_prose_for_phantom_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    """``t_<hex>`` references in ``text`` that resolve neither to a local task nor to a card in the hermes-hub
    replica of other gateways (deduped; advisory). An unreadable/stale replica leaves them unflagged."""
    if not text:
        return []
    from hermes_cli.kanban_board_replica_refs import unresolved_after_replica

    return unresolved_after_replica(_missing_task_refs(conn, dict.fromkeys(_TASK_ID_PROSE_RE.findall(text))))
