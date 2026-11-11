"""Admission of additive receipts at the canonical card writer.

No receipt waives acceptance. Remote observations are bounded reads made before
our transaction; only same-board ownership can be rechecked under its lock.
Historical reads must never run admission again.
"""
from __future__ import annotations

import sqlite3

from hermes_completion_receipt import validate_completion_receipt

OPEN = frozenset(("todo", "ready", "running", "scheduled", "review", "blocked", "triage"))


def pending_owners(receipt):
    return tuple(dict.fromkeys(r["owner"] for item in receipt["items"]
                              for r in item["remaining"] if r["state"] == "pending"))


def owner_parts(ref):
    kind, rest = ref[1:].split(":", 1)
    scope, rest = rest.split("@", 1)
    gateway, identity = rest.split("/", 1)
    return kind, scope, gateway, identity


def refusal(ref, why):
    return ValueError(f"completion_receipt: pending owner {ref}: {why}")


def board_scope(conn):
    from hermes_cli.kanban_db_sync import _db_key, board_slug_for_path
    from hermes_cli.kanban_origin import gateway_names
    return board_slug_for_path(_db_key(conn) or ""), gateway_names()[0]


def _local_link(conn, task_id, owner_id):
    from hermes_cli.kanban_archive_scope import _creator_task_id
    if task_id == owner_id:
        return False
    return bool(conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                             (task_id, owner_id)).fetchone() or _creator_task_id(conn, owner_id) == task_id)


def _remote_link(conn, task_id, scope, gateway, identity):
    from hermes_cli.kanban_awaiting import _latest_park
    awaited, _ = _latest_park(conn, task_id)
    return bool(isinstance(awaited, dict) and awaited.get("kind") == "card"
                and (awaited.get("gateway"), awaited.get("board") or "default", awaited.get("ref")) == (
                    gateway, scope, identity))


def _local_authority(conn, row):
    # A synced worker can execute a foreign card without owning its namespace.
    created_on = row["created_on"] if "created_on" in row.keys() else None
    if not created_on:
        return True
    from hermes_cli.kanban_db_sync import _state
    state = _state(conn)
    own = state["gateway_id"] if state else None
    return created_on == (own or board_scope(conn)[1])


def _check_local(conn, task_id, ref):
    _, _, _, identity = owner_parts(ref)
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
    if (row is None or row["status"] not in OPEN or not _local_authority(conn, row)
            or not _local_link(conn, task_id, identity)):
        raise refusal(ref, "must be an open locally owned linked child or created follow-up, not the completing card")


def _check_external(conn, task_id, ref):
    kind, scope, gateway, identity = owner_parts(ref)
    if kind == "session":
        raise refusal(ref, "a requester/shepherd link is not acceptance of the pending obligation")
    if not _remote_link(conn, task_id, scope, gateway, identity):
        raise refusal(ref, "no existing qualified wait link")
    from hermes_cli.kanban_wait_replica import read_task
    try:
        row = read_task(gateway, identity, scope)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise refusal(ref, f"fresh owner observation unavailable: {exc}") from exc
    if row.get("status") not in OPEN:
        raise refusal(ref, "owner is not open")


def prepare(conn, task_id, metadata):
    """Return copied metadata and locally recheckable owners; reject before any write.

    Legacy metadata is untouched. Refusing unknown ownership is intentional: a
    plausible reference, stale replica, or new created_cards claim is not a link.
    """
    if not isinstance(metadata, dict) or "completion_receipt" not in metadata:
        return metadata, ()
    receipt = validate_completion_receipt(metadata["completion_receipt"])
    checks = []
    owners = pending_owners(receipt)
    if owners:
        board, gateway = board_scope(conn)
        if not gateway:
            raise ValueError("completion_receipt: local gateway identity unavailable")
        for ref in owners:
            kind, scope, target_gateway, _ = owner_parts(ref)
            if kind == "task" and (scope, target_gateway) == (board, gateway):
                _check_local(conn, task_id, ref)
                checks.append((ref, True))
            else:
                _check_external(conn, task_id, ref)
                checks.append((ref, False))
    return {**metadata, "completion_receipt": receipt}, tuple(checks)


def check_session_declaration(receipt, lineage):
    """Resolve pending card owners from existing session ownership links.

    No session-to-session wait store exists. A session reference alone is not
    such a link (and the declaring session cannot own its own pending closeout).
    Unknown/stale observations fail closed; refresh of history never calls this.
    """
    owners = pending_owners(receipt)
    if not owners:
        return
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_session_activity import session_tasks
    from hermes_cli.kanban_wait_replica import read_task
    gateway = gateway_names()[0]
    if not gateway:
        raise ValueError("completion_receipt: local gateway identity unavailable")
    try:
        rows = session_tasks(lineage)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise ValueError("completion_receipt: session ownership inventory unavailable") from exc
    for ref in owners:
        kind, scope, target_gateway, identity = owner_parts(ref)
        if kind != "task":
            raise refusal(ref, "no existing session-to-session ownership link")
        matches = [r for r in rows if (r.get("board"), r.get("gateway") or gateway, r.get("id")) == (
            scope, target_gateway, identity)]
        if len(matches) != 1 or matches[0].get("stale") or matches[0].get("status") not in OPEN:
            raise refusal(ref, "no unique fresh open card linked to this session")
        if target_gateway == gateway:
            _check_session_local(scope, identity, ref)
        else:
            try:
                observed = read_task(target_gateway, identity, scope)
            except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                raise refusal(ref, "fresh owner observation unavailable") from exc
            if observed.get("status") not in OPEN:
                raise refusal(ref, "owner is not open")


def _check_session_local(board, identity, ref):
    from hermes_cli import kanban_session_activity as activity
    paths = [p for p in activity._board_db_paths() if activity._board_slug(p) == board]
    if len(paths) != 1:
        raise refusal(ref, "local board authority is ambiguous or unavailable")
    with activity.cached_ro(paths[0]) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
        if row is None or row["status"] not in OPEN or not _local_authority(conn, row):
            raise refusal(ref, "not an open locally owned card")


def recheck(conn, task_id, owners):
    """Recheck under the completion transaction, before any event/terminal write."""
    for ref, local in owners:
        if local:
            _check_local(conn, task_id, ref)
        else:
            _, scope, gateway, identity = owner_parts(ref)
            if not _remote_link(conn, task_id, scope, gateway, identity):
                raise refusal(ref, "ownership link changed during admission")


class ReceiptEditRefused(ValueError):
    """``kanban edit`` tried to change a completion receipt; only completion admits one."""


def keep_admitted_receipt(conn, run_id, metadata):
    """Edited handoff metadata for ``run_id`` with the run's admitted receipt kept verbatim.

    A completed-run receipt is admitted once, by ``complete_task`` (owners checked, admission
    marker on the completed event). An edit may rewrite other handoff fields, but a different
    ``completion_receipt`` would ride the original admission marker unchecked, so it is refused;
    omitting it keeps the admitted one. ``run_id=None`` (no completed run) has none admitted.
    """
    import json

    prior = None
    if run_id is not None:
        row = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        try:
            stored = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            stored = {}
        prior = stored.get("completion_receipt") if isinstance(stored, dict) else None
    if "completion_receipt" in metadata and metadata["completion_receipt"] != prior:
        raise ReceiptEditRefused("completion_receipt: only kanban complete admits a receipt; "
                                 "edit keeps the admitted one (omit completion_receipt)")
    edited = {k: v for k, v in metadata.items() if k != "completion_receipt"}
    if prior is not None:
        edited["completion_receipt"] = prior
    return edited
