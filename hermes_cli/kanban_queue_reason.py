"""Durable origin-owned worker queue observations, independent of execution authority."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime

EVENT = "worker_queue_reason"
CODES = {"reserved", "draining", "admission_limits", "no_live_candidate", "capability_mismatch",
         "observation_unavailable", "mixed", "board_error", "board_refused", "no_pool_worker",
         # worker-first overflow (docs/worker-first-v1.md): Board reserve observations and the core final gate
         "workers_unknown", "no_eligible_worker", "workers_saturated", "hub_cpu_at_or_above_limit",
         "hub_cpu_unknown", "hub_capacity_full", "hub_sample_already_used", "hub_overflow_reserved",
         "hub_admission_overrun", "hub_cpu_limit_unknown", "hub_cpu_gate_expired", "hub_cpu_sample_stale",
         "hub_cpu_gate_expired_counting", "hub_cpu_pool_limit_unknown"}
_REJECTIONS = {"invalid_policy", "not_creator", "not_claimable", "worker_pool_disabled",
               "unauthorized_candidate", "explicit_pin", "revision_conflict", "not_found"}
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/+-]{1,120}$")
# Origin-side candidate reason: that machine's pickup gate (coordination.claim_refusal) refuses the card, so the
# Board's own per-candidate rollup must not show it as eligible (t_01a1d560c6d3bc3c).
PICKUP_REFUSED = "pickup_gate_refused"
# Board-authored structural return (state "origin"): no configured pool worker is attested to run the card.
_ORIGIN_REASON = re.compile(r"^no pool worker can run it: missing [A-Za-z0-9_.:/+-]{1,120}(, [A-Za-z0-9_.:/+-]{1,120}){0,63}$")


def _tokens(values):
    return sorted({v for v in values if isinstance(v, str) and _TOKEN.fullmatch(v)}) if isinstance(values, list) else []


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 8.64e15


def normalize(reply):
    """Allowlist diagnostic fields; never store raw transport errors or unknown response keys."""
    raw = reply.get("observation") if isinstance(reply, dict) else None
    out = {"version": 1, "code": "observation_unavailable", "candidates": []}
    if isinstance(raw, dict) and raw.get("version") == 1 and raw.get("code") in CODES:
        out["code"] = raw["code"]
        if _number(raw.get("observed_at_ms")):
            out["observed_at_ms"] = raw["observed_at_ms"]
        candidates = raw.get("candidates")
        for c in candidates[:64] if isinstance(candidates, list) else []:
            if not isinstance(c, dict) or not _tokens([c.get("machine")]):
                continue
            item = {"machine": c["machine"], "reasons": _tokens(c.get("reasons"))}
            if labels := _tokens(c.get("missing_labels")):
                item["missing_labels"] = labels
            if "draining" in item["reasons"] and _number(c.get("drain_until_ms")):
                item["drain_until_ms"] = c["drain_until_ms"]
            out["candidates"].append(item)
        out["candidates"].sort(key=lambda c: c["machine"])
    if isinstance(reply, dict) and reply.get("state") in ("queued", "assigned", "origin"):
        out["state"] = reply["state"]
        if _tokens([reply.get("machine")]):
            out["machine"] = reply["machine"]
    if out.get("state") == "origin":
        # The Board repinned the card to its creator in the reserve transaction; the reason is server-authored
        # text with a fixed shape, so only that shape is stored.
        out["code"] = "no_pool_worker"
        reason = reply.get("reason")
        if isinstance(reason, str) and _ORIGIN_REASON.fullmatch(reason):
            out["reason"] = reason
    return out


def _stored(conn, task_id):
    """The last recorded transition (event)."""
    # Origin timestamps travel with the payload; replica-local insertion IDs do not.
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
        "ORDER BY COALESCE(json_extract(payload, '$.recorded_at_ms'), created_at * 1000) DESC, "
        "payload DESC LIMIT 1", (task_id, EVENT)).fetchall()
    return _loads(rows[0][0]) if rows else None


def _loads(text):
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


# A volatile-only change (see ``VOLATILE_REASONS``) is no transition: it replaces this origin-local row instead of
# appending an event and a comment, so ``latest()`` stays the newest observation. No row = the event is newest.
# Local only (never synced): a replica's ``latest()`` is the origin's last transition, as before.
LATEST_TABLE = "kanban_queue_latest"


def _newest(conn, task_id):
    """The newest observation: the local volatile-only one when it postdates the last transition."""
    value = _stored(conn, task_id)
    try:
        row = conn.execute(f"SELECT payload FROM {LATEST_TABLE} WHERE task_id=?", (task_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return value  # nothing newer than the event was ever stored here (a replica, or no flap yet)
    local = _loads(row[0]) if row else None
    if local is None or (value is not None and not (_ms_of(local) > _ms_of(value))):
        return value
    return local


def _ms_of(value) -> float:
    ms = value.get("recorded_at_ms")
    return float(ms) if _number(ms) else 0.0


def latest(conn, task_id):
    """Read the newest observation, qualifying it against current task lifecycle state."""
    value = _newest(conn, task_id)
    if value is None:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if value.get("state") == "origin":
        # Holds while the card stays where the Board returned it (any status, including its run there).
        try:
            pinned = json.loads(row["runs_on"] or "null") if row else None
        except (TypeError, ValueError):
            pinned = None
        applied = bool(row and not row["placement_policy"] and value.get("machine")
                       and pinned == [f"machine:{value['machine']}"])
        # Before the feed carries the Board's repin, the local row is still the unclaimed policy card the reply
        # was recorded against: the return is decided, not historical.
        pending = bool(row and row["status"] == "ready" and row["placement_policy"] == "worker-first-v1"
                       and not row["claim_lock"] and not row["lease_holder"]
                       and _same_lifecycle(conn, task_id, row, value))
        value["current"] = applied or pending
        return value
    value["current"] = bool(row and row["status"] == "ready" and row["placement_policy"] == "worker-first-v1"
                            and not row["claim_lock"] and not row["lease_holder"]
                            and _same_lifecycle(conn, task_id, row, value))
    return value


def _lifecycle(conn, task_id, row):
    return {"runs_on": row["runs_on"],
            "run_count": conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]}


def _same_lifecycle(conn, task_id, row, value):
    current = _lifecycle(conn, task_id, row)
    previous = value.get("lifecycle", {"runs_on": None, "run_count": 0})
    if value.get("state") == "queued" and current["runs_on"] is None:
        current["runs_on"] = previous["runs_on"]
    # A reserve reply precedes the feed. Its matching assignment is not a new wait.
    if value.get("state") == "assigned" and value.get("machine"):
        try:
            if json.loads(current["runs_on"] or "null") == [f"machine:{value['machine']}"]:
                current["runs_on"] = previous["runs_on"]
        except (TypeError, ValueError):
            return False
    return current == previous


def _signature(value):
    # stage/transport/timing are provenance only: they never make an otherwise-identical observation a new event.
    signature = {k: v for k, v in value.items()
                 if k not in ("observed_at_ms", "recorded_at_ms", "current", "stage", "transport", COMMENTED_KEY,
                              *TIMING_KEYS)}
    if value.get("state") == "assigned" and value.get("machine") and "lifecycle" in value:
        # Reading back the assignment promised by this reply is not a transition.
        signature["lifecycle"] = {**value["lifecycle"], "runs_on": value["machine"]}
    elif value.get("state") == "queued" and "lifecycle" in value:
        signature["lifecycle"] = {**value["lifecycle"], "runs_on": None}
    return signature


# Admission reasons the Board re-samples on every reserve (load, run caps, a held reservation slot, a heartbeat
# sample): they flip between ~10 s replies with no change in what the card waits for. t_01a1d5386cb55abf (11-13) got
# 79 comments in 18 min from these flips alone. Anything else (missing labels, capability, draining, liveness, an
# unknown reason) is structural and still a transition.
VOLATILE_REASONS = frozenset({"load_threshold", "memory_threshold", "max_card_runs_reached",
                              "reserved_capacity_held", "last_seen_invalid"})
# At most one volatile-only comment per card per this window (since the last comment), and only for a different
# picture.
REFRESH_MS = 600_000
# A different volatile-only picture this long after the last event is an event without a comment. pickup-stall
# restarts a worker's clock at the newest event whose lifecycle is not pinned there
# (``kanban_pickup_stall._seen_unpinned_here_ms``): a card the Board re-reserves to one worker for seconds at a time
# must be seen unpinned well inside that worker's threshold (default 600 s), or it files "waiting 10 min" for a
# card it held for seconds (t_01a1d5a79bd2a70d; the t_01a1cfdfb6f3f7ea false-alarm class).
SIGHTING_MS = REFRESH_MS // 2
# Carried on an event written without a comment: when the thread last got one, so sightings never delay a refresh.
COMMENTED_KEY = "commented_at_ms"


def _meaning(value):
    """``_signature`` without volatile reasons or the reserved machine: what the card waits for.

    Whether any candidate is eligible stays in it, so all candidates going ineligible (or one coming back) is a
    transition even when only volatile reasons caused it."""
    signature = _signature(value)
    candidates = signature.get("candidates") or []
    signature["eligible"] = any(not c.get("reasons") for c in candidates)
    signature["candidates"] = [{**c, "reasons": [r for r in c.get("reasons") or [] if r not in VOLATILE_REASONS]}
                               for c in candidates]
    if value.get("state") == "assigned":
        signature.pop("machine", None)
        if "lifecycle" in signature:
            signature["lifecycle"] = {**signature["lifecycle"], "runs_on": None}
    return signature


def _reserved(value):
    return value.get("machine") if value.get("state") == "assigned" else None


def _write(previous, value, now_ms, commented_ms):
    """``(event, refresh)`` for an observation that differs from the last transition ``previous``.

    Every meaning change is an event at once. A reservation handed to another worker is an event too: pickup-stall
    reads the events as "seen not pinned here" sightings (``kanban_pickup_stall._seen_unpinned_here_ms``). For the
    same reason a different volatile-only picture ``SIGHTING_MS`` after the last event is an event. ``refresh``: a
    different volatile-only picture ``REFRESH_MS`` after the last comment (``commented_ms``) may be commented.
    Whether a meaning change is commented is ``_comment_due``'s call."""
    if previous is None or _meaning(previous) != _meaning(value):
        return True, False
    if _reserved(previous) != _reserved(value):
        return True, False
    if _signature(previous) == _signature(value):
        return False, False
    if now_ms - commented_ms >= REFRESH_MS:
        return True, True
    return now_ms - _ms_of(previous) >= SIGHTING_MS, False


# A new meaning is commented only once it has held this long (t_01a1d61f47e3f1a0): on 11-13 meanings that reverted
# within about a minute (all candidates briefly ineligible, a worker draining for its own core update, a 41 s Board
# blip) still wrote 42 comments on one card, 14 in 10 min. The event is still appended at once.
HOLD_MS = 120_000
# Origin-local, never synced (like ``LATEST_TABLE``): the last commented observation, when it was commented, and
# since when the current meaning has held. A hold is confirmed by a repeat reply, which is no event, so this cannot
# live in the event log.
COMMENT_TABLE = "kanban_queue_commented"


def _comment_state(conn, task_id, previous):
    """``{"commented", "commented_at_ms", "since_ms"}``; without a stored row (a card queued before the hold
    existed), the last transition is taken as commented, as every meaning change then was."""
    try:
        row = conn.execute(f"SELECT payload FROM {COMMENT_TABLE} WHERE task_id=?", (task_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        row = None
    state = _loads(row[0]) if row else None
    if isinstance(state, dict) and isinstance(state.get("commented"), dict) and all(
            _number(state.get(k)) for k in ("commented_at_ms", "since_ms")):
        return state
    if previous is None:
        return None
    carried = previous.get(COMMENTED_KEY)
    return {"commented": previous, "commented_at_ms": float(carried) if _number(carried) else _ms_of(previous),
            "since_ms": _ms_of(previous)}


def _save_comment_state(conn, task_id, state):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {COMMENT_TABLE} (task_id TEXT PRIMARY KEY "
                 "REFERENCES tasks(id) ON DELETE CASCADE, payload TEXT NOT NULL)")
    conn.execute(f"INSERT OR REPLACE INTO {COMMENT_TABLE} (task_id, payload) VALUES (?, ?)",
                 (task_id, json.dumps(state, ensure_ascii=False)))


def _run_count(value):
    lifecycle = value.get("lifecycle")
    return lifecycle.get("run_count") if isinstance(lifecycle, dict) else None


def _comment_due(previous, value, now_ms, state, refresh):
    """Whether ``value`` gets a thread comment. At once: the card's first observation, a return to origin and a new
    run. A meaning equal to the last commented one only on a volatile ``refresh``. Any other meaning once it has
    held ``HOLD_MS`` (``state["since_ms"]``), checked on repeats too; one that reverts or changes first never."""
    if previous is None or state is None:
        return True
    if _meaning(previous) != _meaning(value) and (
            value.get("state") == "origin" or _run_count(previous) != _run_count(value)):
        return True
    if _meaning(state["commented"]) == _meaning(value):
        return refresh
    return now_ms - state["since_ms"] >= HOLD_MS


def _stamp(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, UTC).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return "unknown"


def describe(value):
    if not value:
        return "observation unavailable"
    if value.get("state") == "origin":
        title = f"Returned to origin {value.get('machine') or 'unknown'}: {value.get('reason') or 'no pool worker can run it'}"
        if value.get("current") is False:
            title = "[historical] " + title
        at = value.get("observed_at_ms", value.get("recorded_at_ms"))
        return title + (f" — observed {_stamp(at)}" if at is not None else "")
    title = {"reserved": "Worker reserved; awaiting authoritative pickup",
             "draining": "Worker draining; awaiting pickup",
             "admission_limits": "Candidates excluded by admission limits",
             "no_live_candidate": "No live candidate",
             "capability_mismatch": "Candidate capabilities do not match",
             "board_error": "Board observation failed",
             "board_refused": "Board placement request refused",
             "mixed": "Candidates excluded for multiple reasons",
             "observation_unavailable": "Queue detail observation unavailable"}.get(value.get("code"), "Queue observation")
    details = []
    for c in value.get("candidates", []):
        text = f"{c['machine']}: {', '.join(c['reasons']) or 'eligible'}"
        if c.get("missing_labels"):
            text += f" (missing {', '.join(c['missing_labels'])})"
        if "draining" in c["reasons"]:
            text += (f"; producer-announced drain deadline {_stamp(c['drain_until_ms'])} (not guaranteed)"
                     if "drain_until_ms" in c else "; drain end time unknown")
        details.append(text)
    if value.get("http_status"):
        details.append(f"HTTP {value['http_status']}")
    for key in ("error_class", "rejection_code"):
        if value.get(key):
            details.append(value[key])
    gate = value.get("pickup_gate")
    if isinstance(gate, dict) and gate.get("reason"):
        details.insert(0, f"pickup gate: {gate['reason']} (`{gate.get('quote') or ''}`)")
    at = value.get("observed_at_ms", value.get("recorded_at_ms"))
    if at is not None:
        details.append(f"observed {_stamp(at)}")
    if value.get("current") is False:
        title = "[historical] Queue observation: " + title
    return title + (" — " + "; ".join(details) if details else "")


def print_field(value, field):
    if value:
        field("queue", describe(value))


def snapshot(conn, task_id):
    """The row and lifecycle a Board request was made against, to fence a later record on (see ``_record``)."""
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return None if row is None else {"row": dict(row), "lifecycle": _lifecycle(conn, task_id, row)}


def _owned_row(conn, task_id, gateway, fence):
    """The row an observation may be recorded against (``fence``'s when given), or None when not origin-owned
    ready policy work. Call inside the write transaction."""
    from hermes_cli import kanban_db_sync as sync

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is not None and fence is not None:
        if fence["row"]["id"] != task_id:
            raise ValueError("fence snapshot is for another task")
        row = fence["row"]
    if (not row or sync.board_mode(conn) != "primary" or row["created_on"] != gateway
            or (sync.sync_state(conn) or {}).get("gateway_id") != gateway
            or row["placement_policy"] != "worker-first-v1" or row["status"] != "ready"
            or row["claim_lock"] or row["lease_holder"]):
        return None
    return row


def _record(conn, task_id, gateway, value, *, fence=None, comment=True):
    """Append one change-only observation, or store a volatile-only one (``_write``); comment it when
    ``_comment_due`` (a new meaning only once it has held ``HOLD_MS``, confirmed by a later reply or repeat).

    ``fence`` (from ``snapshot``) checks the row the Board request was made from instead of the current one: a
    refusal answered against that row stays recorded after a feed pull moved the row on, exactly as it was before
    the placement tick learned to pull on ``not_claimable``. Returns whether an event was appended.
    """
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        row = _owned_row(conn, task_id, gateway, fence)
        if row is None:
            return False
        value = {**value, "lifecycle": fence["lifecycle"] if fence is not None else _lifecycle(conn, task_id, row)}
        newest = _newest(conn, task_id)
        # A repeat is no event, but it still confirms a held meaning (``_comment_due``).
        repeat = bool(newest) and _signature(newest) == _signature(value)
        previous_ms = (newest or {}).get("recorded_at_ms", 0)
        recorded_ms = max(int(time.time() * 1000), int(previous_ms) + 1 if _number(previous_ms) else 0)
        value = {**value, "recorded_at_ms": recorded_ms}
        previous = _stored(conn, task_id)
        state = _comment_state(conn, task_id, previous)
        event, refresh = (False, False) if repeat else _write(
            previous, value, recorded_ms, state["commented_at_ms"] if state else 0)
        if state is not None and previous is not None and _meaning(previous) != _meaning(value):
            state = {**state, "since_ms": recorded_ms}  # the hold restarts at every meaning change
        commented = _comment_due(previous, value, recorded_ms, state, refresh)
        if event and not commented and state is not None:
            value = {**value, COMMENTED_KEY: state["commented_at_ms"]}
        if commented:
            state = {"commented": value, "commented_at_ms": recorded_ms, "since_ms": recorded_ms}
        if state is not None and (event or commented):
            _save_comment_state(conn, task_id, state)
        if event:
            kb._append_event(conn, task_id, EVENT, value)
        elif not repeat:
            conn.execute(f"CREATE TABLE IF NOT EXISTS {LATEST_TABLE} (task_id TEXT PRIMARY KEY "
                         "REFERENCES tasks(id) ON DELETE CASCADE, payload TEXT NOT NULL)")
            conn.execute(f"INSERT OR REPLACE INTO {LATEST_TABLE} (task_id, payload) VALUES (?, ?)",
                         (task_id, json.dumps(value, ensure_ascii=False)))
        if comment and commented:  # a card no pool worker can run gets the owner's repin notice as its comment
            kb.add_comment(conn, task_id, "worker-placement", comment if isinstance(comment, str) else describe(value),
                           origin_gateway=gateway)
        return event


def mark_refused(value, refused):
    """``value`` with :data:`PICKUP_REFUSED` (and the missing labels) on each candidate whose pickup gate refuses the
    card (``refused``: :func:`hermes_cli.kanban_placement_coordination.pickup_refusals`)."""
    for candidate in value.get("candidates", []):
        why = (refused or {}).get(candidate["machine"])
        if why is None:
            continue
        candidate["reasons"] = sorted({*candidate["reasons"], PICKUP_REFUSED})
        if labels := _tokens(why.get("missing_labels")):
            candidate["missing_labels"] = sorted({*candidate.get("missing_labels", []), *labels})
    return value


def record_reply(conn, task_id, gateway, reply, pool=None, refused=None):
    from hermes_cli import kanban_repin_notice as repin

    value = mark_refused(normalize(reply), refused)
    clear_streak(gateway, task_id)  # the Board answered for this card: a transient failure streak is over
    recorded = _record(conn, task_id, gateway, value, comment=not repin.no_pool_worker(value, pool))
    # Every reply, not only a changed one: a card queued before the notice existed gets it on the next pass.
    repin.maybe_notify(conn, task_id, gateway, value, pool)
    return recorded


def try_record(writer, *args, **kwargs):
    """Diagnostics must not suppress reserve/pull or convert a local fault into a Board error."""
    try:
        return writer(*args, **kwargs)
    except Exception as exc:  # health: allow BLE001 -- optional diagnostics cannot control dispatch
        logging.getLogger(__name__).warning("queue observation write failed (%s)", type(exc).__name__)
        return False


STAGES = frozenset({"pull", "reserve", "repin", "recover"})
TIMING_KEYS = ("budget_ms", "elapsed_ms")
TIMING_CAP_MS = 86_400_000


def _real(value):
    """A finite int/float, never a bool, str, complex, arbitrary object or an int too large for a float."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # e.g. 10**400: unusable as a deadline or duration
        return False


def _ms(seconds):
    """Non-negative whole milliseconds capped at one day, or None for a non-real / non-finite input (bool too)."""
    if not _real(seconds):
        return None
    # Clamp in seconds BEFORE scaling: a huge finite float (1e306) would overflow to inf at * 1000.
    return round(min(TIMING_CAP_MS / 1000, max(0, seconds)) * 1000)


def call_budget(hub, started):
    """Seconds left on ``hub``'s absolute monotonic deadline at ``started``, or None when it has no usable one."""
    deadline = getattr(hub, "deadline", None)
    if not _real(deadline):
        return None
    try:
        return deadline - started
    except Exception:  # health: allow BLE001 -- optional diagnostic arithmetic must never stop the Board call
        return None


def call_timing(budget_s, started, ended):
    """Allowlisted provenance for a failed Board call: budget at start (if known) and time to the exception."""
    timing = {}
    try:
        if (budget := _ms(budget_s)) is not None:
            timing["budget_ms"] = budget
        if (elapsed := _ms(ended - started)) is not None:
            timing["elapsed_ms"] = elapsed
    except Exception:  # health: allow BLE001 -- optional timing must never mask the original Board error
        return timing
    return timing


def transport_kind(error):
    """Allowlisted transport subtype from the exception CLASS only; never its message, url or attributes."""
    import http.client
    import urllib.error
    from hermes_cli.kanban_sync_client import BoardBudgetExhausted

    if isinstance(error, urllib.error.URLError) and isinstance(error.reason, BaseException):
        error = error.reason  # unwrap only an exception reason; a str reason stays "other"
    for cls, kind in ((BoardBudgetExhausted, "budget_exhausted"), (TimeoutError, "timeout"),
                      (http.client.RemoteDisconnected, "disconnected"), (ConnectionResetError, "reset"),
                      (ConnectionRefusedError, "refused")):
        if isinstance(error, cls):
            return kind
    return "other"


def attempt_timing(error):
    """Allowlisted timing of the failed HTTP attempt the Board client stamped on ``error``; ``{}`` when absent.

    The stamp is ``(budget_s, elapsed_s)`` seconds (``kanban_sync_client_attempt.note_attempt``). Each value goes
    through the same bounded whole-millisecond rounding/clamping as reserve/repin timing; an unusable one is omitted."""
    timing = {}
    try:
        from hermes_cli.kanban_sync_client_attempt import ATTEMPT_ATTR
        stamp = getattr(error, ATTEMPT_ATTR, None)
        if type(stamp) is not tuple or len(stamp) != 2:
            return timing
        for key, seconds in zip(TIMING_KEYS, stamp):
            if (ms := _ms(seconds)) is not None:
                timing[key] = ms
    except Exception:  # health: allow BLE001 -- optional timing must never mask the original Board error
        return {}
    return timing


def record_pull_error(conn, gateway, *, status=None, error=None):
    """A failed feed read affects all origin-owned ready policy observations.

    Selection is diagnostic only; _record rechecks ownership and lifecycle under
    the write transaction so a concurrent claim/repin cannot acquire a new wait.
    Timing (budget_ms/elapsed_ms) is the failed HTTP attempt stamped on ``error``, never the whole pull.
    """
    from hermes_cli.kanban_sync_client import Resync

    if isinstance(error, Resync):
        clear_pull_streaks(gateway)  # a feed reset is the Board answering the pull, not an outage
    timing = attempt_timing(error)
    rows = conn.execute(
        "SELECT id FROM tasks WHERE placement_policy='worker-first-v1' AND status='ready' "
        "AND created_on=? AND claim_lock IS NULL AND lease_holder IS NULL", (gateway,)).fetchall()
    for row in rows:
        try_record(record_error, conn, row["id"], gateway, status=status, error=error, stage="pull", timing=timing)


def record_error(conn, task_id, gateway, *, status=None, error=None, fence=None, stage=None, timing=None):
    """Record a failed Board pass for one card.

    A transient failure (transport ``OSError``, ``SyncUnavailable`` or an HTTP 5xx) changes the card's queue
    reason, and posts its comment, only on the ``CONFIRM_PASSES``-th consecutive transient failure of the same
    request kind for that card (see ``_streaks``). Every unconfirmed failure is still recorded, as a ``DIAGNOSTIC_EVENT`` carrying the same
    allowlisted provenance (stage/transport/budget_ms/elapsed_ms), so nothing is silently dropped. A Board
    refusal or other non-transient failure records at once, as before.
    """
    value = {"version": 1, "code": "board_error", "candidates": []}
    from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable
    if isinstance(error, Resync):
        return False  # A feed reset is a synchronization signal, not an outage.
    previous = latest(conn, task_id)
    if previous and previous.get("state") == "origin" and previous.get("current"):
        return False  # The Board already returned this card; a feed read error cannot change that decision.
    if error is not None:
        value["error_class"] = ("HubError" if isinstance(error, HubError) else
                                "SyncUnavailable" if isinstance(error, SyncUnavailable) else
                                "OSError" if isinstance(error, OSError) else "unknown")
        if isinstance(error, OSError):
            value["transport"] = transport_kind(error)
    if stage in STAGES:
        value["stage"] = stage
    if isinstance(timing, dict):
        for key in TIMING_KEYS:
            ms = timing.get(key)
            if type(ms) is int and 0 <= ms <= TIMING_CAP_MS:
                value[key] = ms
    if isinstance(error, HubError):
        status = error.status
        try:
            body = json.loads(error.detail)
            code = body.get("error") if isinstance(body, dict) else None
        except (TypeError, ValueError):
            code = None
        if isinstance(code, str) and code in _REJECTIONS:
            if code == "revision_conflict":
                clear_streak(gateway, task_id)  # the Board answered
                return False  # Expected CAS race; retry after sync, without outage/recovery chatter.
            value.update(code="board_refused", rejection_code=code)
    if type(status) is int and 100 <= status <= 599:
        value["http_status"] = status
    if not _transient(value, error):
        if stage != "pull":
            clear_streak(gateway, task_id)  # a Board answer (refusal or other non-outage error) for this card
        return _record(conn, task_id, gateway, value, fence=fence)
    runs = conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    passes = _bump_streak(gateway, task_id, stage, runs)
    if passes < CONFIRM_PASSES:
        _record_diagnostic(conn, task_id, gateway, {**value, "pass": passes}, fence=fence)
        return False
    return _record(conn, task_id, gateway, value, fence=fence)


DIAGNOSTIC_EVENT = "worker_queue_board_error"
CONFIRM_PASSES = 2
_STREAK_CAP = 4096
# Consecutive transient failures per (gateway, task, kind). kind "pull": the dispatcher's feed pull, observed for
# every origin card on every tick, so a successful pull ends it. kind "card": that card's own reserve/repin, observed
# only when the card is attempted, so only a Board answer for that card ends it (a card skipped by the batch cursor
# neither continues nor breaks its streak). Any transient failure keeps the streak: an outage that alternates
# 502/503/timeout is still one outage. Process-local on purpose: a restart can only delay a confirmation by one pass,
# never manufacture one. The sync loop and the dispatcher both reach it, hence the lock. A streak also ends when the
# card ran since its last failure (it left the queue and came back). There is deliberately no wall-clock window: with
# many queued cards the batch cursor retries one card only every few ticks, and a window would hide that outage.
_streaks: dict[tuple[str, str, str], tuple[int, int]] = {}  # -> (passes, run count at the last failure)
_streaks_lock = threading.Lock()


def _transient(value, error):
    from hermes_cli.kanban_sync_client import HubError, SyncUnavailable

    if value.get("code") != "board_error":
        return False  # a Board refusal is an answer, not an outage
    if isinstance(error, HubError):
        return type(error.status) is int and 500 <= error.status <= 599
    return isinstance(error, (OSError, SyncUnavailable))


def _bump_streak(gateway, task_id, stage, runs):
    key = (gateway, task_id, "pull" if stage == "pull" else "card")
    with _streaks_lock:
        held = _streaks.pop(key, None)
        passes = held[0] + 1 if held is not None and held[1] == runs else 1
        _streaks[key] = (passes, runs)
        while len(_streaks) > _STREAK_CAP:
            del _streaks[next(iter(_streaks))]
    return passes


def clear_streak(gateway, task_id):
    """The Board answered this card's reserve/repin (a reply or a refusal): its card streak ends."""
    with _streaks_lock:
        _streaks.pop((gateway, task_id, "card"), None)


def clear_pull_streaks(gateway):
    """A feed pull succeeded: every origin card's pull streak ends."""
    with _streaks_lock:
        for key in [k for k in _streaks if k[0] == gateway and k[2] == "pull"]:
            del _streaks[key]


def _record_diagnostic(conn, task_id, gateway, value, *, fence=None):
    """Append an unconfirmed transient failure; no queue-reason change and no thread comment."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        if _owned_row(conn, task_id, gateway, fence) is None:
            return False
        kb._append_event(conn, task_id, DIAGNOSTIC_EVENT, {**value, "recorded_at_ms": int(time.time() * 1000)})
        return True
