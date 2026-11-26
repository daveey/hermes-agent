"""Read-only card adapters over already-read rows; never admission or receipt authority.

Counts lack execution attempts and source clocks. Their projection deliberately
stays unknown; fetching a snapshot is not a new observation of its workers.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from hermes_cli.execution_observations import epoch, identity_key, project, same_json

_log = logging.getLogger(__name__)

# What a corrupt row, unreadable config or missing store can raise. Optional observation adapters catch
# exactly these (and log the traceback) so a broken adapter never breaks legacy output; a programming
# error outside this set still surfaces.
ADAPTER_ERRORS = (OSError, RuntimeError, LookupError, ArithmeticError, TypeError, ValueError, AttributeError,
                  ImportError, sqlite3.Error)

DONE_PREFIX = "done:"
_STATE = {"running": "running", "ready": "queued", "todo": "queued", "scheduled": "waiting",
          "blocked": "waiting", "triage": "waiting", "review": "waiting", "done": "done",
          "failed": "failed", "cancelled": "cancelled", "archived": "cancelled"}
_STATE_EVENTS = {"blocked": {"blocked"}, "scheduled": {"scheduled"}, "review": {"review_requested"},
                 "done": {"completed"}, "failed": {"failed"}, "cancelled": {"cancelled"}}


def _gateway_name():
    """Configured gateway name from non-secret metadata only; never the hub key file.

    ``kanban.sync.gateway`` first, then the hub's own gateway name (``kanban_origin.gateway_name``:
    hub.db meta, else the hermes-mail config name). Any failure or absence is None (unknown).
    """
    try:
        from hermes_cli import kanban_db_sync as sync

        name = sync.sync_config().get("gateway")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except ADAPTER_ERRORS:
        _log.debug("kanban.sync gateway name unavailable", exc_info=True)
    try:
        from hermes_cli.kanban_origin import gateway_name

        name = gateway_name()
        return name.strip() if isinstance(name, str) and name.strip() else None
    except ADAPTER_ERRORS:
        _log.debug("hub gateway name unavailable", exc_info=True)
        return None


def local_scope():
    """Existing configured gateway name (non-secret) and active profile; None when not evidenced. Never raises."""
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except ADAPTER_ERRORS:
        _log.debug("active profile unavailable", exc_info=True)
        profile = None
    return _gateway_name(), profile


def board_owner_profile(path):
    """Owner profile of a board DB by its root, the hub ``collect_qualified_tasks`` rule; never the reader's.

    ``kanban_home()`` is shared across profiles, so the active profile would give one card different
    identities in different consumers. ``<root>/...`` is ``default``; ``<root>/profiles/<P>/...`` is ``P``.
    A board outside the root is not evidenced (None, so the identity stays unqualified). Never raises.
    """
    try:
        from pathlib import Path

        from hermes_constants import get_default_hermes_root

        rel = Path(path).resolve().relative_to(get_default_hermes_root().resolve()).parts
    except ADAPTER_ERRORS:
        _log.debug("board owner profile unavailable for %r", path, exc_info=True)
        return None
    if rel and rel[0] == "profiles":
        return rel[1] if len(rel) > 2 and rel[1] and not rel[1].startswith(".") else None
    return "default"


def card_scope(path):
    """``(gateway, owner profile)`` for cards read from the board DB at ``path``."""
    return local_scope()[0], board_owner_profile(path)


def conn_path(conn):
    """File path of a connection's main database, or None (in-memory/unreadable)."""
    try:
        for _seq, name, file in conn.execute("PRAGMA database_list"):
            if name == "main":
                return file or None
    except ADAPTER_ERRORS:
        _log.debug("database path unavailable", exc_info=True)
    return None


def _request(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def card(row, *, gateway=None, profile=None, now=None):
    """Compose v1 from a card row without queries or synthetic execution generations.

    Only a current attempt's persisted heartbeat/event can date its state.
    Display-only activity may be from an older attempt and never proves progress.
    Legacy wait labels lack qualified targets/predicates; keep them unknown.
    No existing completion verification reference is exposed by these rows, so
    reported done cannot become verified completion here.
    """
    now = time.time() if now is None else now
    done = row.get("_execution_done")
    if isinstance(done, dict) and not row.get("remote"):
        # Positive delivered-completion evidence (kanban_execution_receipt); nothing else is ever 'done'.
        if row.get("stale"):  # a replayed busy-board list is not a current owner read
            done = {**done, "state": "unknown", "last_known_state": "done", "unknown_reason": "source_unavailable"}
        return project(done, now=now, source_class="sidebar")
    source = row.get("_execution_source")
    if isinstance(source, dict) and not row.get("remote"):
        from hermes_cli.kanban_execution_source import raw
        value = raw(row, source)
        request = _request(row.get("human_request"))
        if request is not None:
            value["human_request"] = request
        if row.get("stale"):
            value.update(state="unknown", unknown_reason="source_unavailable")
        return project(value, now=now, source_class="sidebar")
    if isinstance(row.get("_replica_run"), dict):
        from hermes_cli.kanban_replica_execution import raw as replica_raw
        value = replica_raw(row)
        if value is not None:
            return project(value, now=now, source_class="sidebar")
    return project(card_raw(row, gateway=gateway, profile=profile, now=now), now=now, source_class="sidebar")


def card_raw(row, *, gateway=None, profile=None, now=None):
    """The unprojected ``kanban.card`` record ``card`` projects for a row with no execution source, receipt or
    running replica evidence. The sidebar counts store it for every remote row ``kanban_replica_execution.raw``
    returns None for (parked, todo, unknown, or running with no current-run beat; see ``kanban_replica_execution
    .collect``), so they project exactly what ``session_tasks`` does for the same row."""
    remote = bool(row.get("remote"))
    gateway = row.get("gateway") if remote else gateway
    profile = row.get("owner_profile", row.get("profile")) if remote else profile
    identity = {"kind": "card", "work_id": row.get("id")}
    for key, value in (("gateway", gateway), ("profile", profile), ("board", row.get("board")),
                       ("run_id", row.get("current_run_id"))):
        if value is not None:
            identity[key] = value
    owner = {"gateway": gateway, "profile": profile}
    if row.get("assignee"):
        owner["assignee"] = row["assignee"]
    if row.get("session_id"):
        owner["session"] = row["session_id"]
    reference = "tasks/" + str(row.get("id"))
    at = row.get("last_heartbeat_at") if row.get("status") == "running" else None
    revision = reference + "/last_heartbeat_at/" + str(at) if at is not None else None
    event = row.get("_execution_event")
    if (row.get("status") != "running" and isinstance(event, dict) and row.get("current_run_id") is not None
            and event.get("kind") in _STATE_EVENTS.get(row.get("status"), ())
            and event.get("run_id") == row["current_run_id"] and event.get("id") is not None):
        event_at = event.get("created_at")
        if at is None or (epoch(at) and epoch(event_at) and event_at > at):
            at = event_at
            revision = "task_events/" + str(event["id"])
    request = _request(row.get("human_request"))
    state = _STATE.get(row.get("status"), "unknown")
    if row.get("status") in {"blocked", "triage"} and request is not None:
        state = "needs_user"
    raw = {"version": 1, "identity": identity, "owner": owner, "state": state,
           "source": "kanban.card", "source_revision": revision, "evidence_refs": [revision] if revision else [],
           "observed_at": at, "valid_until": at + 180 if epoch(at) else None,
           "last_activity": row.get("activity_at"), "last_activity_kind": row.get("activity_kind"),
           "last_progress": None, "last_progress_kind": None, "last_progress_ref": None,
           "wait": None}
    if request is not None:
        raw["human_request"] = request
    if row.get("stale"):
        raw.update(state="unknown", last_known_state=state, unknown_reason="source_unavailable")
    elif (remote and row.get('status') == 'ready' and row.get('current_run_id') is None
          and isinstance(row.get('execution_observations'), list)):
        from hermes_cli.kanban_nonrunning_reason import select
        # The acquiring reader owns authentication/lifecycle checks. Preserve
        # only its separately clocked diagnosis, never the old execution phase.
        reason = select(row.get('execution_observations'), {**row, 'profile': profile},
                        now=time.time() if now is None else now)
        if reason is not None:
            raw['non_running_reason'] = reason
    return raw


def _merge(row, observations, *, keep_empty=False):
    previous = row.get("execution_observations")
    previous = previous if isinstance(previous, list) else []
    kept = [value for value in previous if not isinstance(value, dict)
            or value.get("source") not in {"kanban.card", "kanban.counts"}]
    if kept or observations or keep_empty:
        row["execution_observations"] = [*kept, *observations]
    else:
        row.pop("execution_observations", None)


_SCOPE_UNSET = object()


def add_cards(rows, *, now=None, gateway=_SCOPE_UNSET, profile=_SCOPE_UNSET):
    """Attach optional work evidence without replacing independent capacity diagnostics.

    Omit ``gateway``/``profile`` for the legacy process-home scope. Passing either
    (including ``None``) binds the observation identity and never calls ``local_scope``.
    """
    if not rows:
        return rows
    if gateway is _SCOPE_UNSET and profile is _SCOPE_UNSET:
        try:
            gateway, profile = local_scope()
        except ADAPTER_ERRORS:
            _log.debug("execution observation scope unavailable", exc_info=True)
            gateway = profile = None
    else:
        if gateway is _SCOPE_UNSET:
            gateway = None
        if profile is _SCOPE_UNSET:
            profile = None
    for row in rows:
        try:
            if row.get("_execution_error") and not isinstance(row.get("_execution_done"), dict):
                # An independent, readable delivery receipt survives an open-card source failure.
                raise RuntimeError("execution source unavailable")
            observation = card(row, gateway=gateway, profile=profile, now=now)
        except ADAPTER_ERRORS:
            _log.debug("execution observation unavailable for card %s", row.get("id"), exc_info=True)
            observation = failed_card(row, gateway=gateway, profile=profile, now=now)
        _merge(row, [observation])
        row.pop("_execution_event", None)
        row.pop("_execution_source", None)
        row.pop("_execution_done", None)
        row.pop("_execution_error", None)
        row.pop("_execution_scope", None)
        row.pop("_replica_run", None)
    return rows


def _project(value, *, now, **kw):
    out = project(value, now=now, **kw)
    if value.get("unknown_reason") == "adapter_error":
        out.update(state="unknown", unknown_reason="adapter_error")
    return out


def failed_card(row, *, gateway=None, profile=None, now=None):
    """Explicit unknown for a card whose adapter failed: never absent, never a state, keeps its own ask."""
    value = failed_card_raw(row, gateway=gateway, profile=profile)
    return _project(value, now=time.time() if now is None else now, source_class="sidebar")


def failed_card_raw(row, *, gateway=None, profile=None):
    """The unprojected record ``failed_card`` projects. The sidebar counts store it for a remote card whose adapter
    failed (``kanban_replica_execution.collect``), so they project what ``add_cards`` does for the same row."""
    from hermes_cli.kanban_execution_source import unknown_raw
    source = row.get("_execution_source")
    source = source if isinstance(source, dict) else {}
    scope = row.get("_execution_scope")
    # A board-scoped row owns its profile, None (unrooted board: unevidenced) included; it never falls back
    # to the reader's active profile, so list and counts identities agree (C9).
    owned = isinstance(scope, dict) or bool(source)
    if isinstance(scope, dict):
        source = {**scope, **{k: v for k, v in source.items() if v is not None}}
    if row.get("remote"):
        source = {"gateway": row.get("gateway"), "profile": row.get("owner_profile", row.get("profile")),
                  "board": row.get("board")}
    else:
        source = {**source, "gateway": source.get("gateway") or gateway,
                  "profile": source.get("profile") if owned else profile,
                  "board": source.get("board") or row.get("board")}
    return unknown_raw(row, source)


_HISTORY = ("human_request", "retained_human_requests", "delivery_receipt", "historical_delivery_receipts")


def _carry_history(previous, now):
    """Source failed: keep asks/receipts of an execution, each under its own exact identity.

    Records sharing an identity_key are one execution and pool their history (deduplicated). A
    record without a valid identity, or of another generation/run, is never merged into or carried
    for a different execution. A successful read never calls this: it cannot confirm old history.
    """
    groups = {}
    for value in previous if isinstance(previous, list) else []:
        if (isinstance(value, dict) and value.get("source") in {"kanban.card", "kanban.counts"}
                and any(value.get(k) for k in _HISTORY)):
            key = identity_key(value.get("identity"))
            if key is not None:
                groups.setdefault(key, []).append(value)
    carried = []
    for group in groups.values():
        asks, receipts = [], []
        for value in group:
            for pool, one, many in ((asks, "human_request", "retained_human_requests"),
                                    (receipts, "delivery_receipt", "historical_delivery_receipts")):
                for item in [value.get(one), *(value.get(many) or [])]:
                    if isinstance(item, dict) and not any(same_json(item, seen) for seen in pool):
                        pool.append(item)
        base = dict(group[-1])
        base.update(state="unknown", unknown_reason="source_unavailable", retained_human_requests=asks,
                    historical_delivery_receipts=receipts)
        carried.append(project(base, now=now))
    return carried


def count_snapshot(row, keys, *, now=None, evidence=None):
    """Expose exact known card keys from the existing batched counts, without extra I/O.

    Counts carry no run or owner-qualified local gateway. Do not launder the
    count read time into a worker clock, or treat an empty inventory as done.
    """
    rows = []
    evidence = evidence or {}
    # ``done:`` evidence is a done-bucket observation: it is attached, never counted as open work.
    done = {key for key in evidence if key.startswith(DONE_PREFIX)}
    for key in sorted({key for key, bucket, _ in keys if bucket in {"active", "blocked"}} | done):
        if key in evidence:
            rows.append({"_raw": evidence[key]})
            continue
        parts = key.split("/")
        if len(parts) == 4:
            gateway, profile, board, tid = parts
            rows.append({"id": tid, "board": board, "gateway": gateway, "owner_profile": profile, "remote": True})
        elif len(parts) == 3:
            gateway, board, tid = parts
            rows.append({"id": tid, "board": board, "gateway": gateway, "remote": True})
        elif len(parts) == 2:
            rows.append({"id": parts[1], "board": parts[0]})
        else:
            rows.append({"id": parts[-1]})
    now = time.time() if now is None else now
    observations = [_project(value["_raw"], now=now) if "_raw" in value else card(value, now=now) for value in rows]
    if row.get("kanban_unknown"):
        observations.extend(_carry_history(row.get("execution_observations"), now))
        observations.append(project({"version": 1, "source": "kanban.counts", "state": "unknown"}, now=now))
    # Open work with no card record (a pending chat close-out is an obligation, not a card execution) still
    # gets the array: its presence tells consumers this is a current producer, not a legacy one (F8).
    _merge(row, observations, keep_empty=bool(row.get("kanban_active")))
