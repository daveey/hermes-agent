"""Explicit repin of an unstarted worker-first card back to its origin: ``hermes kanban move <id> --runs-on
machine:<origin>``. [daveey]

A ``worker-first-v1`` card's ``runs_on`` and ``placement_policy`` are Board-owned: the Board refuses an ordinary
field edit (``placement_fenced``) and the outbox strips ``runs_on`` from a policy card's push
(:func:`hermes_cli.kanban_worker_placement.acknowledged_outbox`). So a generic ``move_queued`` relabel of such a card
would report success while nothing changes. The one supported change is the Board's explicit CAS repin
(``POST /v2/boards/<id>/placement/repin``, worker ``repinWorker``): creator-only, ``ready`` with no lease, at the
exact acknowledged revision; it clears the policy and its reservation and sets the pin.

Scope (everything else refuses and writes nothing):

* the card is ``worker-first-v1``, ``ready``, created on this gateway, with no local claim, no Board lease, no
  ``started_at`` and no run row (never started anywhere);
* the target is exactly ``[machine:<this gateway>]`` (the origin); another target is refused
  ``repin_unavailable`` instead of a local relabel the Board would strip;
* the board is a Board primary, its ``sync_version`` is acknowledged and no task edit is pending in the outbox;
* the Hub identity used is this board's own gateway.

The local row is never edited here. After the Board answers, the authoritative feed is pulled; until that readback
lands the local policy stays as it was. Refusals leave this local copy intact; a lost reply has unknown Board outcome. A pull failure
after a successful repin is reported as committed with readback pending (the normal sync loop applies it), not as a
failed move. No network call runs inside a DB transaction, and nothing here runs on a timer.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Optional

REPINNED_EVENT_FLAG = "repinned"
_BOARD_CODES = ("revision_conflict", "not_creator", "not_claimable", "invalid_pin", "not_found")
# Repin CAS attempts on revision_conflict, each after a pull of the newest Board row (see repin_to_origin).
CONFLICT_ATTEMPTS = 3


def _refuse(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "reason_code": code, "error": message}


def _unknown(cause: str) -> dict[str, Any]:
    return _refuse("outcome_unknown", f"repin reply unavailable ({cause}); Board outcome unknown, "
                                     "local copy unchanged; verify placement after sync before retrying")


def applies(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when ``move_queued`` must route ``task_id`` here: a policy card whose status a queued move would
    otherwise mutate. Running/done/archived and unknown cards keep the generic path's own refusals."""
    row = conn.execute("SELECT status, placement_policy FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(row is not None and row["placement_policy"] and row["status"] not in ("running", "done", "archived"))


def _board_code(detail: str) -> Optional[str]:
    """A known Board rejection code from an error body, never the raw body."""
    try:
        parsed = json.loads(detail)
        text = str(parsed.get("error") or parsed.get("reason") or "") if isinstance(parsed, dict) else ""
    except (TypeError, ValueError):
        text = str(detail or "")
    for code in _BOARD_CODES:
        if re.search(rf"\b{code}\b", text):
            return code
    return None


def eligibility(conn: sqlite3.Connection, task_id: str, labels: list[str]) -> dict[str, Any]:
    """Local preconditions (read-only, no transaction held). ``{"ok": True, me, board, revision, previous}`` or a
    refusal dict."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_placement as wp

    if sync.board_mode(conn) != "primary":
        return _refuse("not_synced", "a worker-first card is repinned through the Board service (kanban.sync primary)")
    state = sync.sync_state(conn) or {}
    me, board = state.get("gateway_id"), state.get("board_id")
    if not me or not board:
        return _refuse("not_synced", "this board has no Board sync identity")
    row = conn.execute(
        "SELECT status, placement_policy, created_on, claim_lock, lease_holder, started_at, current_run_id, "
        "sync_version, runs_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return _refuse("not_found", f"task {task_id} not found")
    origin = f"machine:{me}"
    unavailable = (f"{task_id} is placed by the worker-first policy; the only supported move is the Board repin "
                   f"of an unstarted ready card to its origin (--runs-on {origin})")
    if row["placement_policy"] != wp.POLICY:
        return _refuse("repin_unavailable", f"{task_id} has unknown placement policy {row['placement_policy']!r}")
    if labels != [origin]:
        return _refuse("repin_unavailable", unavailable + f"; {labels} is not that target")
    if row["created_on"] != me:
        return _refuse("repin_unavailable", unavailable + f"; it was created on {row['created_on']!r}, not {me}")
    if row["status"] != "ready":
        return _refuse("repin_unavailable", unavailable + f"; it is {row['status']}, not ready")
    if row["claim_lock"] is not None or row["lease_holder"] is not None:
        return _refuse("repin_unavailable", unavailable + "; it holds a claim or Board lease")
    started = row["started_at"] is not None or row["current_run_id"] is not None or conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? LIMIT 1", (task_id,)).fetchone() is not None
    if started:
        return _refuse("repin_unavailable", unavailable + "; it has already started")
    if row["sync_version"] is None or conn.execute(
            "SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'tasks' AND row_uid = ? AND state = 'pending' "
            "LIMIT 1", (task_id,)).fetchone() is not None:
        return _refuse("not_acknowledged", f"{task_id} has a task edit the Board has not acknowledged yet; "
                                           "retry after the next sync")
    try:
        previous = json.loads(row["runs_on"]) if row["runs_on"] else None
    except (TypeError, ValueError):
        previous = None
    return {"ok": True, "me": me, "board": board, "revision": int(row["sync_version"]), "previous": previous}


def repin_to_origin(conn: sqlite3.Connection, task_id: str, labels: list[str], reason: str, *,
                    hub: Any = None) -> dict[str, Any]:
    """Board CAS repin + authoritative pull. Result keys match ``move_queued`` plus ``repinned``,
    ``board_version`` and ``readback`` (``confirmed`` | ``pending``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_sync_client as client

    pre = eligibility(conn, task_id, labels)
    if not pre.get("ok"):
        return pre
    me, board = pre["me"], pre["board"]
    try:
        if hub is None:
            ident = client.load_identity()
            if ident.gateway != me:
                return _refuse("identity_mismatch", f"Hub identity {ident.gateway!r} is not this board's gateway {me!r}")
            hub = client.Hub(ident)
        elif getattr(getattr(hub, "ident", None), "gateway", None) != me:
            return _refuse("identity_mismatch", f"Hub identity is not this board's gateway {me!r}")
    except client.SyncUnavailable as exc:
        return _refuse("board_unavailable", f"no Board identity on this gateway ({exc}); policy unchanged")

    reply: Any = None
    for attempt in range(CONFLICT_ATTEMPTS):
        try:
            reply = hub.repin(board, task_id=task_id, expected_revision=pre["revision"], runs_on=labels)
            break
        except client.HubError as exc:
            if exc.status >= 500:
                return _unknown(f"HTTP {exc.status}")
            code = _board_code(exc.detail)
            if code != "revision_conflict":
                return _refuse("repin_refused", f"the Board refused the repin (HTTP {exc.status}"
                                                + (f", {code}" if code else "") + "); local copy unchanged; verify after sync")
            conflict = _refuse("revision_conflict", f"the Board has a newer revision of {task_id} than this copy; "
                                                    "verify placement after the next sync before retrying")
            if attempt + 1 >= CONFLICT_ATTEMPTS:
                return conflict
            # A queued policy card's Board row is rewritten by every reservation reply, so the local revision is
            # often a step behind. Read the newest row, re-check every precondition on it, and CAS again: never a
            # blind write over an edit this copy has not seen.
            try:
                client.pull(conn, hub, board, observers=True, partition=me)
            except (client.HubError, client.Resync, client.SyncUnavailable, OSError):
                return conflict
            pre = eligibility(conn, task_id, labels)
            if not pre.get("ok"):
                return pre
        except (OSError, client.Resync) as exc:
            return _unknown(type(exc).__name__)
    version = reply.get("version") if isinstance(reply, dict) else None

    # The Board has committed. Record the operator's reason as the card's move audit (no runs_on/policy write).
    move_no = len(km.history(conn, task_id)) + 1
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, km.MOVED_EVENT, {
                "from_machine": me, "runs_on": labels, "previous_runs_on": pre["previous"], "reason": reason[:500],
                "move_no": move_no, "forced": False, "matches": [me], "queued": True, REPINNED_EVENT_FLAG: True,
                "board_version": version})
    except sqlite3.Error:
        pass  # the Board's own placement_repinned event still records the change

    out = {"ok": True, "runs_on": labels, "matches": [me], "move_no": move_no, "status": "ready",
           REPINNED_EVENT_FLAG: True, "board_version": version, "readback": "pending"}
    try:
        client.pull(conn, hub, board, observers=True, partition=me)
    except (client.HubError, client.Resync, client.SyncUnavailable, OSError) as exc:
        out["note"] = (f"the Board committed the repin (revision {version}); reading it back failed "
                       f"({type(exc).__name__}), so the local copy still shows the policy until the next sync")
        return out
    row = conn.execute("SELECT status, placement_policy, runs_on FROM tasks WHERE id = ?", (task_id,)).fetchone()
    try:
        local = json.loads(row["runs_on"]) if row and row["runs_on"] else None
    except (TypeError, ValueError):
        local = None
    if row is not None and row["placement_policy"] is None and local == labels:
        out["readback"] = "confirmed"
        out["status"] = row["status"]
    else:
        out["note"] = (f"the Board committed the repin (revision {version}); the local copy has not caught up yet "
                       "and updates on the next sync")
    return out
