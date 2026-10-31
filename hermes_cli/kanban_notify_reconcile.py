"""Receipt-gated retirement of historical wrong-local refused-send subscriptions. [daveey]

Before session-mentions resolved a refused chat's owner, a Kanban worker refused a send into a bare
``@session:<profile>/<id>`` ref for a chat on ANOTHER gateway got a ``platform='tui'`` row on its own
board here. Nothing on this gateway can drain it (the unloaded-session sweeper only finds local chats),
so ``kanban_session_activity._unseen_on`` counts it forever and the chat-closeout watchdog files a
false "unseen" incident, while the chat's owner gateway already delivered the result through the
``platform='hub'`` link (incident t_01a18f20b8f29e9e, 2026-10-30).

This maintenance API never sends, wakes, or advances cursors. Callers supply readers backed by the
authoritative destination gateway (its own session store, not a replica). A row is removed only when
the chat is not local, the owner is known, the hub route exists, every pending event is a completion
and the owner's transcript lineage holds its exact ``remote:<gw>:<profile>/<board>/<task>:<event>``
notice key. Any other outcome leaves the row (and its unseen count) in place, retryable.
"""
from __future__ import annotations

from collections.abc import Callable
import logging
import sqlite3

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as notify
from hermes_cli.kanban_session_activity import UNSEEN_KINDS


def reconcile_refused_sub(
    conn: sqlite3.Connection, *, task_id: str, chat_id: str, thread_id: str = "",
    source_gateway: str, source_profile: str, board: str,
    local_session_exists: Callable[[str, str], bool],
    owner: Callable[[str, str], tuple[str, str] | None],
    delivered_keys: Callable[[str, str, str, set[str]], set[str]],
) -> str:
    """Remove one erroneous tui row only after exact remote receipt readback.

    ``owner(profile, chat)`` must check the owner gateway's session store; None
    means unknown. ``local_session_exists`` must raise on unreadable stores.
    ``delivered_keys`` reads transcript event keys across compression lineage,
    not admission/ledger status or free text. All three are read-only readers.
    The correct hub subscription must already exist. Re-check the complete row
    and event range under the writer txn so racing completion/claim cannot lose
    a notice. Outcomes are stable diagnostics; repeated success is ``absent``.
    """
    key = (task_id, "tui", chat_id, thread_id)
    row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?", key).fetchone()
    if row is None:
        return "absent"
    snapshot = dict(row)
    if notify._decode_notify_delivery_metadata(snapshot["delivery_metadata"]).get("reason") != "send_refused":
        return "provenance_mismatch"
    if snapshot.get("inflight_from") is not None:
        return "inflight"
    profile = snapshot.get("notifier_profile") or "default"
    try:
        if local_session_exists(profile, chat_id):
            return "local_owner"
        destination = owner(profile, chat_id)
        if not destination or destination[0] == source_gateway or destination[1] != profile:
            return "unknown_owner"
        gateway, profile = destination
        hub_chat = f"{gateway}/{profile}/{chat_id}"
        if not _hub_sub(conn, task_id, hub_chat, thread_id):
            return "missing_remote_route"
        events = _pending(conn, task_id, snapshot["last_event_id"])
        if not events:  # nothing unseen: harmless, and no receipt can justify touching it
            return "nothing_pending"
        # This API only reconciles completed notices with stable remote event keys.
        # Other event kinds need their own receipt identity, never a cursor guess.
        if any(kind != "completed" for _, kind in events):
            return "unsupported_events"
        keys = {f"remote:{source_gateway}:{source_profile}/{board}/{task_id}:{eid}" for eid, _ in events}
        if not keys <= delivered_keys(gateway, profile, chat_id, keys):
            return "missing_receipt"
    except Exception:  # caller-supplied cross-gateway readers: any failure means "no evidence", never a removal
        logging.getLogger(__name__).exception("notify reconcile: evidence unavailable for %s/%s", task_id, chat_id)
        return "evidence_unavailable"
    with kb.write_txn(conn):
        current = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=? AND platform=? AND chat_id=? AND thread_id=?", key).fetchone()
        if current is None:
            return "absent"
        if dict(current) != snapshot or _pending(conn, task_id, snapshot["last_event_id"]) != events:
            return "changed_retry"
        if not _hub_sub(conn, task_id, hub_chat, thread_id):
            return "changed_retry"
        notify.remove_notify_sub(conn, task_id=task_id, platform="tui", chat_id=chat_id, thread_id=thread_id)
        kb._append_event(conn, task_id, "notify_reconciled", {"reason": "remote_transcript_receipt", "chat_id": chat_id,
                                                        "owner": gateway, "event_keys": sorted(keys)})
    return "reconciled"


def _pending(conn, task_id, cursor):
    ph = ",".join("?" for _ in UNSEEN_KINDS)
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT id, kind FROM task_events WHERE task_id=? AND id>? AND kind IN ({ph}) ORDER BY id",
        (task_id, cursor, *UNSEEN_KINDS))]


def _hub_sub(conn, task_id, chat_id, thread_id):
    return conn.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id=? AND platform='hub' AND chat_id=? AND thread_id=?",
                        (task_id, chat_id, thread_id)).fetchone() is not None
