"""Conservative replacement-key absence across a moving Board snapshot. [daveey]

A complete UID-ordered scan plus every change from its first head through its
last head covers every live row at the latter cut. Inserts behind the scan cursor
and edits to already-read rows are in that interval. We veto even transient key
matches, rather than reconstructing state or treating deletions as permission.
This is fresh observational evidence, not an atomic key reservation.
"""
from __future__ import annotations

from typing import Any

from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable

MAX_FEED_PAGES = 500
_INCOMPLETE = "authoritative_snapshot_incomplete"


def cover_changes(hub: Any, board: str, key: str, first: int, cut: int) -> str | None:
    """Prove the finite scan interval complete; never chase a continuously moving head."""
    cursor = first
    for _ in range(MAX_FEED_PAGES):
        try:
            page = hub.changes(board, cursor)
        except (HubError, Resync, SyncUnavailable, OSError, AttributeError, ValueError) as exc:
            return f"authoritative_snapshot_unavailable: {exc}"
        if (not isinstance(page, dict) or page.get("resync") or page.get("error")
                or type(page.get("head")) is not int or page["head"] < cut
                or type(page.get("next")) is not int or type(page.get("more")) is not bool
                or not isinstance(page.get("changes"), list)):
            return _INCOMPLETE + ": malformed interval feed"
        previous = cursor
        for change in page["changes"]:
            if (not isinstance(change, dict) or type(change.get("seq")) is not int
                    or change["seq"] != previous + 1 or change["seq"] > page["head"]
                    or not isinstance(change.get("uid"), str) or not change["uid"]
                    or not isinstance(change.get("table"), str)
                    or change.get("op") not in ("upsert", "delete", "lease")):
                return _INCOMPLETE + ": malformed or gapped interval feed"
            previous = change["seq"]
            if change["table"] == "tasks" and change["op"] != "delete":
                fields = change.get("fields")
                if (change["op"] != "upsert" or not isinstance(fields, dict)
                        or "idempotency_key" not in fields or fields.get("uid") != change["uid"]):
                    return _INCOMPLETE + ": incomplete task change"
                if fields["idempotency_key"] == key:
                    return "replacement_key_in_remote_use"
        if (page["next"] != previous or previous <= cursor
                or (not page["more"] and previous != page["head"])):
            return _INCOMPLETE + ": incomplete interval feed"
        if previous >= cut:
            return None
        cursor = previous
    return _INCOMPLETE + ": interval page cap reached"
