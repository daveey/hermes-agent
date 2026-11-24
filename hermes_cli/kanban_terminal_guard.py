"""A done or archived card leaves that status only through an explicit reopen.

Invariant: a card whose latest terminal event (``completed``, ``archived``, or a ``status``/``status_healed`` move
to ``done``/``archived``) has no reopen ordered after it (``reopened``, ``unblocked``, ``review_reopened``,
``promoted_manual``, or a ``status`` move to a live status), in origin order (``_event_order``: second, UUIDv7 ms
when the uid was minted in that second, then id), stays terminal. A wake (``continuation_woken``,
``condition_met``, ``promoted``, ``wake_check_*``) is never a reopen, whatever order it was minted or delivered in.

Why: a wake minted against a stale copy of the card carries ``status=ready``. Two producers made one in Nov 2026:
a MacBook whose pull was hours behind fired a clock wake for a card a worker had completed meanwhile (pushed 5 h
later, t_01a1d1092f3caa2f), and worker-1 fired wakes from a local copy that still read ``scheduled`` for six cards
done for days (t_01a1c7b27ca3b496 and five more). The push stamps its HLC at send time, so that ``status`` won the
Board's per-field LWW over the completion, and the Board leased the card again.

Enforced where a status is produced or applied: the wake producers (``kanban_continuation``,
``kanban_wake_check``) skip a held card; ``kanban_db.unblock_task`` refuses one (``unblocked`` is a reopen, and its
automated callers act on the local row); the feed apply (``kanban_db_sync.apply_remote_changes``) keeps a held card
terminal; the push (``kanban_sync_client.push``) strips the stale status; the Board (hermes-hub rows.ts/
transitions.ts) refuses the lease and puts the card back. Each ignored wake leaves a ``stale_wake_ignored`` event
naming the terminal event it lost to. [daveey]
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

TERMINAL_STATUSES = frozenset({"done", "archived"})
STALE_WAKE_IGNORED = "stale_wake_ignored"
# The event that made the card terminal -> the status it left the card in.
_TERMINAL_KINDS = {"completed": "done", "archived": "archived"}
# Explicit reopens. ``status`` (an operator/orchestrator status edit) counts when it names a live status.
REOPEN_KINDS = frozenset({"reopened", "unblocked", "review_reopened", "promoted_manual"})
WAKE_KINDS = frozenset({"continuation_woken", "condition_met", "promoted", "wake_check_restored",
                        "wake_check_broken", "wake_check_lost"})
_STATUS_KINDS = ("status", "status_healed")


def _payload(raw: Any) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _status_named(event: dict) -> Optional[str]:
    """The status a ``status``/``status_healed`` event moved the card to."""
    payload = _payload(event.get("payload"))
    value = payload.get("to") if event["kind"] == "status_healed" else payload.get("status")
    return value if isinstance(value, str) else None


def terminal_status(event: dict) -> Optional[str]:
    """The terminal status this event put the card in, or None when it is not a terminal move."""
    if event["kind"] in _TERMINAL_KINDS:
        return _TERMINAL_KINDS[event["kind"]]
    if event["kind"] in _STATUS_KINDS:
        status = _status_named(event)
        return status if status in TERMINAL_STATUSES else None
    return None


def is_reopen(event: dict) -> bool:
    if event["kind"] in REOPEN_KINDS:
        return True
    if event["kind"] in _STATUS_KINDS:
        status = _status_named(event)
        return bool(status) and status not in TERMINAL_STATUSES
    return False


_LIFECYCLE_KINDS = tuple(sorted({*_TERMINAL_KINDS, *REOPEN_KINDS, *_STATUS_KINDS}))
_EVENTS_SQL = ("SELECT id, {uid}, kind, payload, created_at FROM task_events WHERE task_id = ? "
               f"AND kind IN ({','.join('?' * len(_LIFECYCLE_KINDS))})")


def _events(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """The card's terminal and reopen events. One indexed query; a pre-uid schema (no ``task_events.uid``) is the
    rare fallback, so it costs a failed prepare there instead of a ``PRAGMA`` on every call."""
    try:
        rows = conn.execute(_EVENTS_SQL.format(uid="uid"), (task_id, *_LIFECYCLE_KINDS))
    except sqlite3.OperationalError as exc:
        if "uid" not in str(exc):
            raise
        rows = conn.execute(_EVENTS_SQL.format(uid="NULL AS uid"), (task_id, *_LIFECYCLE_KINDS))
    return [dict(r) for r in rows]


def terminal_hold(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """The card's latest terminal event when no explicit reopen is ordered after it, else None.

    ``{"id", "uid", "kind", "status", "created_at"}``: the event a wake must lose to."""
    from hermes_cli.kanban_sync_divergence import _event_order
    events = _events(conn, task_id)
    terminals = [e for e in events if terminal_status(e)]
    if not terminals:
        return None
    last = max(terminals, key=_event_order)
    order = _event_order(last)
    if any(is_reopen(e) and _event_order(e) > order for e in events):
        return None
    return {"id": last["id"], "uid": last["uid"], "kind": last["kind"], "status": terminal_status(last),
            "created_at": last["created_at"]}


def _note(conn: sqlite3.Connection, task_id: str, hold: dict, **extra: Any) -> None:
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, task_id, STALE_WAKE_IGNORED, {
        "terminal_event": hold["uid"] or hold["id"], "terminal_kind": hold["kind"],
        "terminal_status": hold["status"], **{k: v for k, v in extra.items() if v is not None}})


def _noted(conn: sqlite3.Connection, task_id: str, hold: dict, source: str) -> bool:
    """True when this card already recorded an ignored wake from ``source`` against this terminal event."""
    ref = hold["uid"] or hold["id"]
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? AND id > ?",
                               (task_id, STALE_WAKE_IGNORED, hold["id"])):
        payload = _payload(raw)
        if payload.get("terminal_event") == ref and payload.get("source") == source:
            return True
    return False


def skip_wake(conn: sqlite3.Connection, task_id: str, wake_kind: str, **extra: Any) -> bool:
    """Wake producers, inside their write txn: True (do nothing) when the card is held terminal.

    The local row can still read ``scheduled`` (a stale copy: the completion reached the events but not the row,
    or the row was restored from an older copy), so the event log decides, not the row. Records one
    ``stale_wake_ignored`` per producer and terminal event, so a due clock does not write one every tick."""
    hold = terminal_hold(conn, task_id)
    if hold is None:
        return False
    source = f"producer:{wake_kind}"
    if not _noted(conn, task_id, hold, source):
        _note(conn, task_id, hold, source=source, wake_kind=wake_kind, **extra)
    return True


_UNBLOCKABLE = ("blocked", "scheduled", "triage")


def unblock_held(conn: sqlite3.Connection, task_id: str, *, event_extra=None) -> bool:
    """``kanban_db.unblock_task``, first inside its write txn: True (refuse) when the row reads blocked/scheduled/
    triage but the card is held terminal. ``unblocked`` is an explicit reopen (``REOPEN_KINDS``, and on the Board),
    so an automated caller acting on a stale copy (shared-blocker clear/probe, quota release, ``resolved_check``)
    would otherwise reopen a done card. Spend stops also require a recorded promote/archive decision.
    A card that is not unblockable anyway leaves no note."""
    from hermes_cli.kanban_spend_decision import pending as spend_pending
    from hermes_cli.kanban_selfwork_exception import missing_local_adapter
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row is not None and row[0] in _UNBLOCKABLE and (
        skip_wake(conn, task_id, "unblocked") or spend_pending(conn, task_id)
        or missing_local_adapter(conn, task_id, event_extra))


def stale_hold(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """Read-only: the terminal hold of a row that still reads blocked/scheduled/triage (a stale copy), else None."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return terminal_hold(conn, task_id) if row is not None and row[0] in _UNBLOCKABLE else None


def unblock_refusal(conn: sqlite3.Connection, task_id: str, *, verb: str = "unblock") -> Optional[str]:
    """The user-facing text for an unblock ``unblock_held`` refused (CLI, dashboard, operator call), else None:
    the row does read blocked/scheduled, so a bare "not blocked/scheduled?" would mislead. A held-done copy is
    revived by ``reopen`` (``kanban_db_operator.reopen_done_task`` accepts it); nothing revives an archived card.
    ``verb="reopen"``: the CLI reopen's refusal, which names only the archived hold (reopen accepts a done one)."""
    hold = stale_hold(conn, task_id)
    if hold is None or (verb == "reopen" and hold["status"] == "done"):
        if verb == "unblock":
            from hermes_cli.kanban_spend_decision import unblock_refusal as spend_refusal
            return spend_refusal(conn, task_id)
        return None
    head = f"cannot {verb} {task_id}: its {hold['kind']} event holds it {hold['status']} (this row is a stale copy)"
    if hold["status"] == "done":
        return f"{head}; use `hermes kanban reopen {task_id} --reason <why>` to revive it"
    return f"{head}; an archived card is not revived by unblock or reopen"


def push_status_is_stale(conn: sqlite3.Connection, task_id: str, status: Any) -> Optional[dict]:
    """The push: the terminal event a task row's live ``status`` must lose to, or None to send it.

    A status a local wake set before the card's completion reached this file has no reopen after that completion,
    so sending it would reopen the card on the Board (the push stamps its HLC at send time). ``running`` is a claim
    the Board granted, not a wake: never stripped here."""
    if not isinstance(status, str) or status in TERMINAL_STATUSES or status == "running":
        return None
    return terminal_hold(conn, task_id)


def note_push_strip(conn: sqlite3.Connection, task_id: str, hold: dict, status: str) -> None:
    if not _noted(conn, task_id, hold, "push"):
        _note(conn, task_id, hold, source="push", ignored_status=status)


def strip_stale_push(conn: sqlite3.Connection, op: dict) -> dict:
    """One wire op of the push, inside its write txn: a task row whose ``status`` a held card must not take loses
    that field (the rest of the row still goes). A card the Board has not acknowledged (``sync_version`` NULL) keeps
    it: its create needs a status, and the Board has no terminal copy to reopen."""
    fields = op.get("fields")
    if op.get("table") != "tasks" or op.get("op") != "upsert" or not isinstance(fields, dict) \
            or "status" not in fields:
        return op
    row = conn.execute("SELECT sync_version FROM tasks WHERE id = ?", (op.get("uid"),)).fetchone()
    if row is None or row[0] is None:
        return op
    hold = push_status_is_stale(conn, op["uid"], fields["status"])
    if hold is None:
        return op
    note_push_strip(conn, op["uid"], hold, fields["status"])
    return {**op, "fields": {k: v for k, v in fields.items() if k != "status"}}


def board_may_hold_terminal(conn: sqlite3.Connection, hub: Any, board: str, row: dict, seen: dict) -> bool:
    """The push before the feed head (t_01a1d36ddcea59fe): True when this outbox row must wait for the pull.

    :func:`strip_stale_push` only sees a completion this file already pulled. A round pushes before its pulls, and
    while a page budget leaves it behind the head, so a live ``status`` on an acknowledged card is checked against
    the Board's own row first: a done/archived row there (or no answer) holds it until a pull reaches the head and
    the strip can judge it. ``seen`` caches one Board read per card per push."""
    if row.get("table_name") != "tasks" or row.get("op") != "upsert":
        return False
    status = _payload(row.get("fields")).get("status")
    if not isinstance(status, str) or status in TERMINAL_STATUSES or status == "running":
        return False
    acked = conn.execute("SELECT sync_version FROM tasks WHERE id = ?", (row["row_uid"],)).fetchone()
    reader = getattr(hub, "row_evidence", None)
    if acked is None or acked[0] is None or reader is None:  # a create has no Board copy to reopen
        return False
    uid = row["row_uid"]
    if uid not in seen:
        from hermes_cli.kanban_sync_client import HubError
        try:
            board_row = reader(board, "tasks", uid).get("row") or {}
            seen[uid] = board_row.get("status") in TERMINAL_STATUSES
        except (HubError, OSError, ValueError, AttributeError):
            seen[uid] = True  # unknown: wait for the pull, which the strip can judge
    return seen[uid]


# --- feed apply ---------------------------------------------------------------------------------------------------

def enforce_after_apply(conn: sqlite3.Connection, left_terminal: Iterable[tuple[str, str]],
                        reopened: Iterable[str]) -> dict[str, str]:
    """End of one feed page, inside the apply transaction (capture suppressed): returns {task id: new status}.

    ``left_terminal``: (task id, terminal status it had) for cards this page moved out of done/archived. A card held
    terminal goes back to that status, unless the Board has a claim on it (``running``: the Board decided, and a
    stale lease is fenced on the Board itself). The ignored status is kept on the ``stale_wake_ignored`` event.
    ``reopened``: cards that received a reopen event in this page. A card this guard held while its reopen was
    still in a later page (a page boundary between the row and its event) gets the held status back."""
    changed: dict[str, str] = {}
    for task_id, _was in left_terminal:
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None or row[0] in TERMINAL_STATUSES or row[0] == "running":
            continue
        hold = terminal_hold(conn, task_id)
        if hold is None:
            continue
        # The hold's own status, not ``_was``: ``terminal_hold`` only returns an event with a terminal status.
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (hold["status"], task_id))
        _note(conn, task_id, hold, source="apply", held_status=row[0])
        changed[task_id] = hold["status"]
    for task_id in reopened:
        if task_id in changed:
            continue
        restored = _restore_held(conn, task_id)
        if restored:
            changed[task_id] = restored
    return changed


def _restore_held(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row[0] not in TERMINAL_STATUSES or terminal_hold(conn, task_id) is not None:
        return None
    # The newest hold the apply guard wrote. A note from another source (a push strip, a producer skip) can sit
    # between the held row's page and the reopen's page and says nothing about what the apply held back.
    last = conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? "
                        "AND json_extract(payload, '$.source') = 'apply' ORDER BY id DESC LIMIT 1",
                        (task_id, STALE_WAKE_IGNORED)).fetchone()
    if last is None:
        return None
    held = _payload(last[1]).get("held_status")
    if not isinstance(held, str) or held in TERMINAL_STATUSES:
        return None
    # Only a hold newer than every terminal event: an older one belongs to an earlier completion. Local ids
    # (arrival order) on purpose, unlike ``terminal_hold``'s origin order: the question is whether this apply note
    # was written after the terminal event reached this file, which only arrival order answers.
    newest_terminal = max((e["id"] for e in _events(conn, task_id) if terminal_status(e)), default=0)
    if last[0] < newest_terminal:
        return None
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (held, task_id))
    return held
