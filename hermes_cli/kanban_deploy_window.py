"""Deploy waits read the host's core update window before they count as a repeated park. [daveey]

``hermes-sync`` on a host batches core updates into windows and publishes the batch it is in to
``<root>/cache/core-update/window.json`` (the ROOT home: every profile on the host shares one
update). A card waiting on a core deploy (``awaiting.kind == 'deploy'``) used to re-park every
time it woke before the batch opened, and its third park escalated to an "unstick" owner card
although the deploy was merely queued.

The record (schema ``version`` 1) is evidence only when it is fresh (``observed_at`` at most
:data:`FRESH_SECONDS` old, never in the future), names this host (the publisher's ``HOST`` is
``socket.gethostname().split('.')[0]``) and is well typed. Then:

* the park's wake is floored to ``window_at + drain_bound_s`` (the batch's latest end), and its
  reason names the window in Pacific time;
* the park is *healthy*, and neither counts toward nor triggers re-park escalation, only while
  the batch is ``batching``/``draining`` AND either the window is still ahead or the publisher's
  semantic state (``state``, ``state_since``) moved since the previous park on this premise. A
  newer ``observed_at`` alone is not progress: an overdue batch nobody is running escalates.
  The exemption is also bounded in time: once the card's first park in its trailing run on this
  premise is more than :data:`HEALTHY_MAX_SECONDS` old, parks count again (``healthy_expired``).
  Nothing checks that the awaited ref is in the batch, so a ref that never reaches main would
  otherwise re-park as healthy forever while other commits keep the window moving.

Anything else (missing, stale, foreign, malformed) changes nothing: the park escalates as before.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional

WINDOW_FILE = Path("cache", "core-update", "window.json")
VERSION = 1
FRESH_SECONDS = 1200
HEALTHY_STATES = ("batching", "draining")
HEALTHY_MAX_SECONDS = 24 * 3600  # a deploy-park run older than this is no longer exempt
DISPLAY_ZONE = "America/Los_Angeles"
_INT_FIELDS = ("observed_at", "window_at", "state_since", "drain_bound_s")


def host_identity() -> str:
    """This host's name exactly as the window publisher writes it."""
    return socket.gethostname().split(".")[0]


def window_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / WINDOW_FILE


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_window(now: int) -> Optional[dict]:
    """The validated window record for this host, or None when there is no usable evidence."""
    try:
        data = json.loads(window_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION or not _is_int(data.get("version")):
        return None
    if not all(_is_int(data.get(key)) for key in _INT_FIELDS):
        return None
    if not isinstance(data.get("host"), str) or not isinstance(data.get("state"), str):
        return None
    if data["host"] != host_identity() or data["drain_bound_s"] < 0:
        return None
    if data["observed_at"] > now or now - data["observed_at"] > FRESH_SECONDS or data["state_since"] > now:
        return None
    return {key: data[key] for key in ("host", "observed_at", "window_at", "state", "state_since",
                                       "drain_bound_s")}


def _previous(conn, task_id: str, premise: str) -> Optional[dict]:
    """``deploy_window`` of the latest park on ``premise`` (None when it carried none)."""
    for (payload,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued') "
            "ORDER BY id DESC LIMIT 20", (task_id,)):
        try:
            got = json.loads(payload or "{}").get("awaiting") or {}
        except (ValueError, AttributeError):
            continue
        if isinstance(got, dict) and got.get("premise") == premise:
            seen = got.get("deploy_window")
            return seen if isinstance(seen, dict) else None
    return None


def _streak_since(conn, task_id: str, premise: str, probe: Optional[str]) -> Optional[int]:
    """``created_at`` of the first park in this card's trailing run of parks on ``premise`` (or on
    the same opaque ``probe``), with the same stop rules as ``kanban_awaiting.prior_same_premise``:
    another premise or the last escalation ends the run. Unbounded on purpose: a 20-event window
    would keep the run's start under a day old however long the card has really waited."""
    from hermes_cli.kanban_awaiting import ESCALATED_EVENT

    first = None
    for kind, payload, created_at in conn.execute(
            "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? "
            "AND kind IN ('scheduled','continued',?) ORDER BY id DESC", (task_id, ESCALATED_EVENT)):
        if kind == ESCALATED_EVENT:
            break
        try:
            got = json.loads(payload or "{}").get("awaiting") or {}
        except (ValueError, AttributeError):
            got = {}
        if not isinstance(got, dict) or got.get("premise") is None:
            continue
        if got["premise"] != premise and not (probe and got.get("probe") == probe):
            break
        first = created_at
    return first


def annotate(conn, task_id: str, record: dict, *, now: int) -> None:
    """Attach ``deploy_window`` (with its ``healthy`` verdict) to a deploy park's record, in place."""
    if record.get("kind") != "deploy" or not record.get("premise"):
        return
    window = read_window(now)
    if window is None:
        return
    prev = _previous(conn, task_id, record["premise"])
    moved = bool(prev) and (prev.get("state"), prev.get("state_since")) != (window["state"], window["state_since"])
    since = _streak_since(conn, task_id, record["premise"], record.get("probe"))
    window["streak_since"] = int(since) if since is not None else now
    window["healthy"] = window["state"] in HEALTHY_STATES and (window["window_at"] > now or moved)
    if window["healthy"] and now - window["streak_since"] > HEALTHY_MAX_SECONDS:
        # Nothing ties the awaited ref to the pending batch, so other commits can keep a window
        # ahead forever. Past the bound the exemption ends and REPARK_LIMIT escalates as usual.
        window["healthy"] = False
        window["healthy_expired"] = True
    record["deploy_window"] = window


def is_healthy(record) -> bool:
    seen = record.get("deploy_window") if isinstance(record, dict) else None
    return isinstance(seen, dict) and seen.get("healthy") is True


def _local_time(epoch: int) -> str:
    from zoneinfo import ZoneInfo

    when = datetime.fromtimestamp(epoch, ZoneInfo(DISPLAY_ZONE))
    return f"{when.hour % 12 or 12}:{when:%M %p %Z}"


def apply(record, wake_at: Optional[int], reason: Optional[str], *, now: int) -> tuple[Optional[int], Optional[str]]:
    """``(wake_at, reason)`` for the parking event: floored to the batch's drain end and naming the
    window when the record carries window evidence; unchanged otherwise."""
    seen = record.get("deploy_window") if isinstance(record, dict) else None
    if not isinstance(seen, dict) or wake_at is None:
        return wake_at, reason
    from hermes_cli.kanban_timed_park import MAX_WAKE_SECONDS

    floor = min(seen["window_at"] + seen["drain_bound_s"], now + MAX_WAKE_SECONDS)
    note = f"waiting for {seen['host']}'s core update window at {_local_time(seen['window_at'])}"
    if note not in (reason or ""):
        reason = f"{reason}; {note}" if reason else note
    return max(int(wake_at), floor), reason
