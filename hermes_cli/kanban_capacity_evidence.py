"""Advisory dispatch evidence; never read by admission or scheduling decisions.

One bounded latest snapshot per local board, not a replicated authority. Reads
never write or renew source validity. Unknown admission coverage is explicit.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import uuid

from hermes_cli.execution_observations import adapt, capacity_reason, epoch

_log = logging.getLogger(__name__)
TABLE = "kanban_capacity_observation"
TASK_LIMIT = 128


def _lifecycle(conn, task_id):
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    count = conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    return {"status": row["status"], "run_id": row["current_run_id"],
            "runs_on": row.get("runs_on"), "run_count": count, "assignee": row.get("assignee"),
            "parents": [list(p) for p in kb.parent_states(conn, task_id)]}


def _board(conn):
    from hermes_cli import kanban_db_sync as sync
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    return sync.board_slug_for_path(path)


def _reason(code, native, reference, phase="pre_claim"):
    return capacity_reason(code, native_code=native, phase=phase, evidence_ref=reference)


def _native_restrictions(result, tid, reference):
    """Keep unmapped admission restrictions without inventing a capacity category."""
    rows = []
    if result.at_capacity:
        rows.append({"source_field": "at_capacity", "native_code": result.at_capacity, "evidence_ref": reference})
    for name in ("claim_deferred", "capsule_refused", "owner_down", "skipped_per_profile_capped", "respawn_guarded"):
        for item in getattr(result, name):
            if item[0] == tid:
                native = item[2] if name == "owner_down" else item[1]
                if name == "skipped_per_profile_capped":
                    native = name
                # Native diagnostic tokens only, never arbitrary error text.
                native = native if isinstance(native, str) and re.fullmatch(r"[A-Za-z0-9_.:/=-]{1,160}", native) else name
                rows.append({"source_field": name, "native_code": native, "evidence_ref": reference})
    # Intentional bounded local automatic-claim defer (claim-quiet lease): not capacity, a stopped dispatcher
    # or withheld remote work. Episode and lease deadline keep the age/episode identity.
    for task, episode in getattr(result, "quiet_deferred", ()):
        if task == tid:
            row = {"source_field": "quiet_deferred", "native_code": "quiet_claim", "evidence_ref": reference,
                   "episode_id": str(episode)[:160]}
            deadline = getattr(result, "quiet_deadlines", {}).get(episode)
            if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
                row["deadline_at"] = deadline
            rows.append(row)
    return rows


def _task_reasons(result, tid, reference):
    reasons = []
    for task, native in result.skipped_ineligible:
        if task == tid:
            reasons.append(_reason("placement", native, reference))
    for task, native in result.claim_deferred:
        if task == tid and native in {"labels_mismatch", "pinned_only", "offline"}:
            reasons.append(_reason("placement", native, reference))
    if tid in result.rate_limited:
        # A reaped submitter can refuse before its managed worker ever starts.
        reasons.append(_reason("provider_limit", "rate_limited", reference, "post_claim"))
    # Only explicit provider quota clocks; generic cooldowns are not provider limits.
    held = result.clock_held.get(tid)
    if held and held[0] in {"subscription_quota_reset", "rate_limit_cooldown", "pinned_quota_wall"}:
        reasons.append(_reason("provider_limit", held[0], reference))
    for task, native in result.respawn_guarded:
        if task == tid and native in {"subscription_quota_reset", "rate_limit_cooldown"}:
            reason = _reason("provider_limit", native, reference)
            if reason not in reasons:
                reasons.append(reason)
    return reasons


def _provider_phase(conn, tid, reasons, now):
    """A reaped rate-limit result is post-start only with its own bound receipt."""
    if not any(r["native_code"] == "rate_limited" for r in reasons):
        return
    from hermes_cli.kanban_capacity_metrics import attempts
    refusal = conn.execute("SELECT id, run_id, created_at FROM task_events "
                           "WHERE task_id=? AND kind='rate_limited' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    if refusal is None or refusal["run_id"] is None:
        return
    events = [dict(e) for e in conn.execute(
        "SELECT id, kind, created_at, run_id, payload FROM task_events "
        "WHERE task_id=? AND run_id=? AND id<=? ORDER BY id DESC LIMIT 256",
        (tid, refusal["run_id"], refusal["id"])).fetchall()][::-1]
    rows = attempts(events, board=_board(conn), task_id=tid, now=now)
    matched = next((r for r in rows if r["run_id"] == refusal["run_id"]
                    and r["claim_to_process_start_s"] is not None
                    and r["process_start_at"] <= refusal["created_at"] <= now), None)
    if matched:
        for reason in reasons:
            if reason["native_code"] == "rate_limited":
                reason.update(phase="post_start", run_id=refusal["run_id"],
                              evidence_ref=f"task_event:{_board(conn)}:{tid}:{refusal['id']}",
                              process_ref=matched["process_ref"])


def record(conn, result, *, board, machine, now=None, update_drain=False, dry_run=False):
    """Record only genuine dispatch observations; a failed diagnostic cannot stop dispatch.

    No free-slot or ready-eligibility inference from registry caps/status counts.
    Coverage stays unknown until a same-interval producer proves those facts.
    """
    if dry_run or result.skipped_locked or result.skipped_owned:
        return False
    from hermes_cli import kanban_db as kb

    now = time.time() if now is None else now
    board = _board(conn)
    revision = str(uuid.uuid4())
    reference = f"dispatch:{machine}:{board}:{revision}"
    common = []
    if result.memory_pressure:
        common.append(_reason("memory_pressure", result.memory_pressure, reference))
    if update_drain:
        common.append(_reason("update_drain", "suppress_spawn", reference))
    try:
        # A deferred snapshot keeps multi-query reads coherent without taking
        # the writer lock. Release it before the single-row write transaction.
        conn.execute("SAVEPOINT capacity_snapshot")
        try:
            rows = conn.execute(
                "SELECT id, assignee, status, current_run_id, claim_lock FROM tasks "
                "WHERE status IN ('ready','todo','running','review','scheduled','blocked') "
                "ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END, id LIMIT ?", (TASK_LIMIT + 1,)).fetchall()
            tasks = {}
            for row in rows[:TASK_LIMIT]:
                tid = row["id"]
                reasons = _task_reasons(result, tid, reference)
                _provider_phase(conn, tid, reasons, now)
                parents = kb.unsatisfied_parents(conn, tid)
                if parents:
                    reasons.append(_reason("dependency", "unsatisfied_parents", reference))
                elif row["status"] == "ready" and not row["claim_lock"]:
                    reasons = common + reasons
                event = conn.execute("SELECT max(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
                tasks[tid] = {"lifecycle": _lifecycle(conn, tid), "reasons": reasons,
                              "native_restrictions": _native_restrictions(result, tid, reference) if row["status"] == "ready" else [],
                              "parents": parents, "assignee": row["assignee"], "source_event": event}
            value = {"version": 1, "observed_at_ms": now * 1000, "valid_until_ms": (now + 180) * 1000,
                     "source": "kanban.dispatch", "source_revision": revision, "machine": machine,
                     "board": board, "evidence_ref": reference, "tasks": tasks,
                     "machine_reasons": common,
                     "coverage_truncated": len(rows) > TASK_LIMIT}
        finally:
            conn.execute("RELEASE capacity_snapshot")
        with kb.write_txn(conn):
            conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            conn.execute(f"INSERT INTO {TABLE} (id,payload) VALUES(1,?) "
                         "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (json.dumps(value),))
        return True
    except (sqlite3.Error, RuntimeError, TypeError, ValueError):
        _log.warning("capacity observation write unavailable", exc_info=True)
        return False


def observe_dispatch(conn, result, *, board, dry_run=False, update_drain=False):
    """Best-effort post-result seam; diagnostics never change dispatch's return."""
    try:
        import socket
        from hermes_cli import kanban_db_sync as sync
        machine = (sync.sync_state(conn) or {}).get("gateway_id") or socket.gethostname()
        record(conn, result, board=_board(conn), machine=machine,
               dry_run=dry_run, update_drain=update_drain)
    except Exception:  # health: allow BLE001 -- optional evidence cannot control admission
        _log.warning("capacity observation unavailable", exc_info=True)
    # One durable reason per passed-over card and ready episode (kanban-dd62aef001); never raises. [daveey]
    from hermes_cli.kanban_dispatch_skip import record_tick
    record_tick(conn, result, dry_run=dry_run)


def _unknown(reason, **fields):
    return {"version": 1, "freshness": "unknown", "unknown_reason": reason,
            "reasons": [], "eligible_backlog": None, "admissible_slots": None,
            "unknown_coverage": [reason], **fields}


def read(conn, task, *, board=None, now=None):
    """Read a lifecycle-qualified observation without manufacturing freshness."""
    actual_board = _board(conn)
    if board is not None and board != actual_board:
        return _unknown("missing_or_foreign_observation")
    board = actual_board
    now = time.time() if now is None else now
    if task is None:
        return _unknown("missing_task")
    try:
        row = conn.execute(f"SELECT payload FROM {TABLE} WHERE id=1").fetchone()
        value = json.loads(row[0]) if row else None
    except (sqlite3.Error, TypeError, ValueError):
        return _unknown("observation_unavailable")
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("board") != board:
        return _unknown("missing_or_foreign_observation")
    stored = value.get("tasks", {}).get(task.id)
    if not stored:
        truncated = value.get("coverage_truncated", False)
        return _unknown("task_coverage_truncated" if truncated else "task_not_observed",
                        coverage_truncated=truncated)
    source = {k: value[k] for k in ("observed_at_ms", "valid_until_ms", "source", "source_revision",
                                   "machine", "board", "evidence_ref")}
    if stored["lifecycle"] != _lifecycle(conn, task.id):
        return _unknown("lifecycle_changed", **source)
    identity = {"gateway": value["machine"], "profile": stored["assignee"], "board": board,
                "kind": "card", "work_id": task.id}
    if stored["lifecycle"]["run_id"] is not None:
        identity["run_id"] = stored["lifecycle"]["run_id"]
    # Neither a poll/event id nor task.status is an execution generation or a
    # live process receipt. An unclaimed card's optional envelope is unknown.
    state = {"ready": "queued", "todo": "waiting"}.get(task.status, "unknown")
    observation = adapt({"version": 1, "identity": identity,
                         "owner": {"gateway": value["machine"], "profile": stored["assignee"]},
                         "state": state, "source": value["source"], "source_revision": value["source_revision"],
                         "observed_at": value["observed_at_ms"] / 1000,
                         "valid_until": value["valid_until_ms"] / 1000,
                         "evidence_refs": [value["evidence_ref"]], "diagnostics": stored["reasons"],
                         "last_activity": None, "last_progress": None}, now=now, source_class="sidebar")
    at, until = source["observed_at_ms"] / 1000, source["valid_until_ms"] / 1000
    freshness = "unknown"
    if all(epoch(v) for v in (at, until, now)) and at <= now and until >= at:
        freshness = "current" if now < min(until, at + 180) else "stale"
    out = {"version": 1, **source, "task_id": task.id, "lifecycle": stored["lifecycle"],
           "coverage_truncated": value.get("coverage_truncated", False),
           "freshness": freshness, "reasons": stored["reasons"],
           "native_restrictions": stored.get("native_restrictions", []),
           "machine_reasons": value.get("machine_reasons", []),
           "eligible_backlog": False if stored["parents"] and freshness == "current" else None,
           "admissible_slots": None, "unknown_coverage": ["admission_not_fully_observed"],
           "execution_observation": observation}
    if observation.get("unknown_reason"):
        out["execution_unknown_reason"] = observation["unknown_reason"]
    return out


# Restrictions of the observing dispatcher's machine, not of the card: a card pinned
# elsewhere waits for another machine, so the note names the one that observed them.
MACHINE_CODES = frozenset({"capacity.memory_pressure", "capacity.update_drain"})
MACHINE_FIELDS = frozenset({"at_capacity"})


def describe(value):
    """Never turn an incomplete restriction list into an assertion of availability."""
    machine = value.get("machine")

    def on(text, scoped):
        return f"{text} on {machine}" if scoped and isinstance(machine, str) and machine else text

    codes = ", ".join(on(r["code"] + " (" + r["native_code"] + "; " + r["phase"] + ")", r["code"] in MACHINE_CODES)
                      for r in value.get("reasons", []))
    native = ", ".join(on(r.get("source_field", "restriction") + " (" + r["native_code"] + ")",
                          r.get("source_field") in MACHINE_FIELDS)
                       for r in value.get("native_restrictions", []))
    codes = "; ".join(v for v in (codes, native) if v)
    return (f"{value.get('freshness', 'unknown')}: {codes or 'restriction coverage incomplete'}; "
            "admissible capacity unknown")
