"""Durable origin-owned worker queue observations, independent of execution authority."""
from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import UTC, datetime

EVENT = "worker_queue_reason"
CODES = {"reserved", "draining", "admission_limits", "no_live_candidate", "capability_mismatch",
         "observation_unavailable", "mixed", "board_error", "board_refused", "no_pool_worker"}
_REJECTIONS = {"invalid_policy", "not_creator", "not_claimable", "worker_pool_disabled",
               "unauthorized_candidate", "explicit_pin", "revision_conflict", "not_found"}
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/+-]{1,120}$")
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
    # Origin timestamps travel with the payload; replica-local insertion IDs do not.
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? AND json_valid(payload) "
        "ORDER BY COALESCE(json_extract(payload, '$.recorded_at_ms'), created_at * 1000) DESC, "
        "payload DESC LIMIT 1", (task_id, EVENT)).fetchall()
    try:
        value = json.loads(rows[0][0]) if rows else None
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def latest(conn, task_id):
    """Read the last transition, qualifying it against current task lifecycle state."""
    value = _stored(conn, task_id)
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
    signature = {k: v for k, v in value.items() if k not in ("observed_at_ms", "recorded_at_ms", "current")}
    if value.get("state") == "assigned" and value.get("machine") and "lifecycle" in value:
        # Reading back the assignment promised by this reply is not a transition.
        signature["lifecycle"] = {**value["lifecycle"], "runs_on": value["machine"]}
    elif value.get("state") == "queued" and "lifecycle" in value:
        signature["lifecycle"] = {**value["lifecycle"], "runs_on": None}
    return signature


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
             "mixed": "Candidates have different exclusions",
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
    at = value.get("observed_at_ms", value.get("recorded_at_ms"))
    if at is not None:
        details.append(f"observed {_stamp(at)}")
    if value.get("current") is False:
        title = "[historical] Queue observation: " + title
    return title + (" — " + "; ".join(details) if details else "")


def print_field(value, field):
    if value:
        field("queue", describe(value))


def _record(conn, task_id, gateway, value):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync

    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (not row or sync.board_mode(conn) != "primary" or row["created_on"] != gateway
                or (sync.sync_state(conn) or {}).get("gateway_id") != gateway
                or row["placement_policy"] != "worker-first-v1" or row["status"] != "ready"
                or row["claim_lock"] or row["lease_holder"]):
            return False
        value = {**value, "lifecycle": _lifecycle(conn, task_id, row)}
        previous = _stored(conn, task_id)
        if previous and _signature(previous) == _signature(value):
            return False
        previous_ms = (previous or {}).get("recorded_at_ms", 0)
        recorded_ms = max(int(time.time() * 1000), int(previous_ms) + 1 if _number(previous_ms) else 0)
        value = {**value, "recorded_at_ms": recorded_ms}
        kb._append_event(conn, task_id, EVENT, value)
        kb.add_comment(conn, task_id, "worker-placement", describe(value), origin_gateway=gateway)
        return True


def record_reply(conn, task_id, gateway, reply):
    return _record(conn, task_id, gateway, normalize(reply))


def try_record(writer, *args, **kwargs):
    """Diagnostics must not suppress reserve/pull or convert a local fault into a Board error."""
    try:
        return writer(*args, **kwargs)
    except Exception as exc:  # health: allow BLE001 -- optional diagnostics cannot control dispatch
        logging.getLogger(__name__).warning("queue observation write failed (%s)", type(exc).__name__)
        return False


def record_pull_error(conn, gateway, *, status=None, error=None):
    """A failed feed read affects all origin-owned ready policy observations.

    Selection is diagnostic only; _record rechecks ownership and lifecycle under
    the write transaction so a concurrent claim/repin cannot acquire a new wait.
    """
    rows = conn.execute(
        "SELECT id FROM tasks WHERE placement_policy='worker-first-v1' AND status='ready' "
        "AND created_on=? AND claim_lock IS NULL AND lease_holder IS NULL", (gateway,)).fetchall()
    for row in rows:
        try_record(record_error, conn, row["id"], gateway, status=status, error=error)


def record_error(conn, task_id, gateway, *, status=None, error=None):
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
    if isinstance(error, HubError):
        status = error.status
        try:
            body = json.loads(error.detail)
            code = body.get("error") if isinstance(body, dict) else None
        except (TypeError, ValueError):
            code = None
        if isinstance(code, str) and code in _REJECTIONS:
            if code == "revision_conflict":
                return False  # Expected CAS race; retry after sync, without outage/recovery chatter.
            value.update(code="board_refused", rejection_code=code)
    if type(status) is int and 100 <= status <= 599:
        value["http_status"] = status
    return _record(conn, task_id, gateway, value)
