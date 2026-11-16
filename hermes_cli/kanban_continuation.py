"""Checkpoint + continue for Kanban runs that would otherwise die at ``max_runtime_seconds``.

A worker that is not going to finish inside its cap (or is about to sit in a long wait) ends
its run with ``continue_task``: the run closes with outcome ``continued`` and the card goes
straight back to ``ready`` (or to ``scheduled`` with a wake time when it is only waiting), with
the worker's checkpoint rendered at the top of the next run's context. The next run also gets
the b78e9861e6 transcript tail from ``kanban_worker_recovery``.

A continuation is neither a failure nor a block: ``consecutive_failures`` and
``block_recurrences`` are left exactly as they were, so it never trips the give-up breaker or
the unblock-loop detector. Its own bound is ``MAX_CONSECUTIVE_CONTINUATIONS`` trailing
``continued`` runs, after which the worker must complete or block.

The soft deadline itself is worker-side (``tools.kanban_tools.inject_soft_deadline_from_env``):
at ``soft_deadline_fraction`` of the cap the running turn gets an OUT-OF-BAND steer telling it to
checkpoint. The dispatcher's hard kill (``enforce_max_runtime``) is unchanged and still fires at
100% for a worker that ignores it.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable, Any, Optional

from hermes_cli.kanban_workspace_publication import publication_guard

CONTINUED_OUTCOME = "continued"
CONTINUATION_WOKEN_EVENT = "continuation_woken"
DEFAULT_SOFT_DEADLINE_FRACTION = 0.85
# Bound on back-to-back continuations without a completion (12 x the 4 h default = 48 h).
MAX_CONSECUTIVE_CONTINUATIONS = 12
# A wait handed off with ``resume_after_seconds`` may not park the card longer than this.
MAX_RESUME_AFTER_SECONDS = 24 * 3600
MAX_CHECKPOINT_CHARS = 12000


def soft_deadline_fraction(cfg: Optional[dict] = None) -> float:
    """``kanban.soft_deadline_fraction`` clamped to [0, 0.99]; 0 disables the soft deadline."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    raw = ((cfg or {}).get("kanban") or {}).get("soft_deadline_fraction", DEFAULT_SOFT_DEADLINE_FRACTION)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOFT_DEADLINE_FRACTION
    return 0.0 if value <= 0 else min(value, 0.99)


def max_consecutive_continuations(cfg: Optional[dict] = None) -> int:
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    raw = ((cfg or {}).get("kanban") or {}).get("max_continuations", MAX_CONSECUTIVE_CONTINUATIONS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_CONSECUTIVE_CONTINUATIONS


def consecutive_continuations(conn: sqlite3.Connection, task_id: str) -> int:
    """Trailing closed runs with outcome ``continued`` (a hard timeout/crash in between does not
    reset the streak; a completed or human-unblocked run does)."""
    rows = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    count = 0
    for row in rows:
        outcome = row[0]
        if outcome == CONTINUED_OUTCOME:
            count += 1
        elif outcome in ("timed_out", "crashed", "oom_killed", "signaled", "reclaimed", "rate_limited"):
            continue
        else:
            break
    return count


@publication_guard
def continue_task(
    conn: sqlite3.Connection, task_id: str, *, checkpoint: str,
    resume_after_seconds: int = 0, expected_run_id: Optional[int] = None,
    max_continuations: Optional[int] = None, metadata: Optional[dict] = None,
    wake_check: Optional[str] = None, check_every: Optional[int] = None, clock_only: bool = False,
    awaiting: Any = None, in_txn: Optional[Callable[[sqlite3.Connection, int], None]] = None,
    blocker_id: Optional[str] = None, blocker_provider: Optional[str] = None,
    blocker_model: Optional[str] = None, blocker_lane: Optional[str] = None,
) -> dict[str, Any]:
    """End the running run as ``continued`` and re-queue the card.

    ``resume_after_seconds`` 0 -> back to its source phase (``ready``, or ``review`` for a
    reviewer run) at once; > 0 -> ``scheduled`` until ``wake_due_continuations`` promotes it.
    Returns ``{"ok": True, "status", "run_id", "continuation", "wake_at"}`` or
    ``{"ok": False, "error"}``. Never touches ``consecutive_failures`` / ``block_recurrences``.
    ``in_txn(conn, run_id)`` runs inside the same write transaction once the run has ended
    (:mod:`hermes_cli.kanban_move` rewrites ``runs_on`` there), so the re-queued card is never
    visible with its old placement. [daveey]
    """
    from hermes_cli import kanban_db as kb

    checkpoint = (checkpoint or "").strip()
    if not checkpoint:
        return {"ok": False, "error": "checkpoint is required"}
    if len(checkpoint) > MAX_CHECKPOINT_CHARS:
        checkpoint = checkpoint[:MAX_CHECKPOINT_CHARS] + "\n… [checkpoint truncated]"
    try:
        resume_after = int(resume_after_seconds or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "resume_after_seconds must be an integer"}
    if resume_after < 0 or resume_after > MAX_RESUME_AFTER_SECONDS:
        return {"ok": False, "error": f"resume_after_seconds must be between 0 and {MAX_RESUME_AFTER_SECONDS}"}
    limit = max_continuations if max_continuations is not None else max_consecutive_continuations()
    now = int(time.time())
    from hermes_cli import kanban_park_pin as kpp
    pin = kpp.plan(conn, task_id, checkpoint)  # git reads stay outside the write txn
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"task {task_id} not found"}
        if row["status"] != "running" or row["current_run_id"] is None:
            return {"ok": False, "error": f"task {task_id} is {row['status']}, not running"}
        if expected_run_id is not None and int(row["current_run_id"]) != int(expected_run_id):
            return {"ok": False, "error": "this run no longer owns the task"}
        streak = consecutive_continuations(conn, task_id)
        if streak >= limit:
            kb._append_event(conn, task_id, "continuation_exhausted",
                             {"continuation": streak, "limit": limit, "checkpoint": checkpoint},
                             run_id=int(row["current_run_id"]))
            return {"ok": False, "reason_code": "continuation_exhausted", "block_kind": "transient",
                    "error": (
                f"continuation limit reached ({streak} consecutive continued runs, limit {limit}). "
                "Finish with kanban_complete, or kanban_block(kind='transient', "
                "reason='continuation_exhausted: split the work or reset the continuation budget') "
                "for agent triage. Pass that reason verbatim: put the handoff state in a kanban_comment "
                "first, because any other text in the reason is read as a human ask and refused "
                "without human_request. Budget resets and task splitting are agent work, not "
                "needs_input. Use needs_input only for an actual user decision, with human_request.")}
        source_status = kb._retry_status_for_run(conn, task_id)
        new_status = "scheduled" if resume_after > 0 else source_status
        wake_at = now + resume_after if resume_after > 0 else None
        from hermes_cli import kanban_wake_check as kwc
        record = None
        if wake_at is not None:
            from hermes_cli import kanban_awaiting as kaw
            try:
                record = kaw.gate(conn, task_id, awaiting=awaiting, reason=checkpoint, wake_check=wake_check,
                                  clock_only=clock_only, op="continue", wake_in=resume_after,
                                  blocker_id=blocker_id or (metadata or {}).get("blocker_id"))
            except kaw.AwaitingError as exc:
                return {"ok": False, "error": str(exc), "reason_code": exc.code}
            except kaw.RepeatedPark as rp:
                owner = kaw.escalate(conn, task_id, record=rp.record, reason=checkpoint,
                                     expected_run_id=int(row["current_run_id"]))
                if owner is None:
                    return {"ok": False, "error": "task changed state concurrently"}
                return {"ok": True, "status": "todo", "run_id": None, "continuation": streak,
                        "wake_at": None, "escalated_to": owner}
            from hermes_cli import kanban_deploy_window as kdw
            wake_at, noted = kdw.apply(record, wake_at, checkpoint, now=now)
            checkpoint = noted or checkpoint
            noted, check_every = kaw.resource_apply(record, checkpoint, check_every)
            checkpoint = noted or checkpoint
            wake_check = wake_check or kaw.card_probe(record)
            why = kaw.park_cycle_error(conn, task_id, record, wake_check)
            if why:
                return {"ok": False, "error": why, "reason_code": kaw.INVALID_AWAITING}
        try:
            wake_check, check_every = kwc.validate_park(
                wake_at=wake_at, wake_check=wake_check,
                check_every=check_every, clock_only=clock_only)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        # A successful checkpoint supersedes stale auth/quota diagnostics, not counters.
        from hermes_cli.kanban_source_receipt import with_source_receipt
        cur = with_source_receipt(conn, task_id, lambda: conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, last_heartbeat_at = NULL, "
            "last_failure_error = NULL "
            "WHERE id = ? AND status = 'running' AND current_run_id = ?",
            (new_status, task_id, int(row["current_run_id"])),
        ))
        if cur.rowcount != 1:
            return {"ok": False, "error": "task changed state concurrently"}
        kpp.apply(conn, task_id, pin)  # unpushed state keeps a pooled card on this worker
        from hermes_cli.kanban_blocker_metadata import blocker_metadata, FIELDS
        meta = blocker_metadata(conn, task_id, reason=checkpoint, metadata=metadata,
                                blocker_id=blocker_id, blocker_provider=blocker_provider,
                                blocker_model=blocker_model, blocker_lane=blocker_lane)
        meta.update({"checkpoint": checkpoint, "continuation": streak + 1,
                     "resume_after_seconds": resume_after, "resume_status": source_status})
        if wake_at is not None:
            meta["wake_at"] = wake_at
        run_id = kb._end_run(
            conn, task_id, outcome=CONTINUED_OUTCOME, status=CONTINUED_OUTCOME,
            summary=kb._first_line(checkpoint, 400) or "checkpoint", metadata=meta,
        )
        from hermes_cli import kanban_run_end_lease
        kanban_run_end_lease.note(conn, task_id, run_id)  # [daveey] free the Board lease after commit
        payload = {"continuation": streak + 1, "status": new_status, "resume_status": source_status}
        # Server-derived ordering proof for PR comments sharing this checkpoint's second.
        # Persist inside the same transaction; caller metadata cannot override it.
        payload["comment_id_at_checkpoint"] = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?", (task_id,),
        ).fetchone()[0]
        if meta.get("blocker_id"):
            payload["blocker_id"] = meta["blocker_id"]
            payload.update({key: meta[key] for key in FIELDS if key in meta})
        from hermes_cli import kanban_wake_check as kwc  # condition wake [daveey]
        from hermes_cli.kanban_wake_check_hub_relay import relay_host
        host = relay_host(conn, task_id, wake_check)  # a hub card's board probe runs on the hub
        if wake_at is not None:
            payload["wake_at"] = wake_at
            payload.update(kwc.park_fields(wake_check, check_every, clock_only, conn=conn, probe_host=host))
            payload["awaiting"] = record
        kb._append_event(conn, task_id, CONTINUED_OUTCOME, payload, run_id=run_id)
        kwc.set_check(conn, task_id, wake_check if wake_at is not None else None, check_every, now=now,
                      register_here=kwc.probe_here(conn, host))
        if in_txn is not None:
            in_txn(conn, run_id)
    return {"ok": True, "status": new_status, "run_id": run_id,
            "continuation": streak + 1, "wake_at": wake_at}


# The only event kinds that put a card INTO ``scheduled`` (``continue_task`` with a wait, and
# ``schedule_task``). Every other kind -- comments, heartbeats, notifications, edits, links,
# attachments -- is an annotation on the parked card and must not mask a due wake. [daveey]
SCHEDULED_ENTRY_EVENTS = (CONTINUED_OUTCOME, "scheduled")
# ``wake_due_continuations`` runs every dispatcher tick; a wake this late means it is stuck.
OVERDUE_WAKE_GRACE_SECONDS = 600


def pending_wake_payload(kind: Optional[str], payload: Any) -> Optional[dict]:
    """The parking payload when the event that parked the card is a timed wait, else None: a
    ``continued`` wait, or a ``scheduled`` timed park from ``kanban_timed_park``. [daveey]"""
    from hermes_cli import kanban_db as kb

    if kind not in SCHEDULED_ENTRY_EVENTS:
        return None
    data = payload if isinstance(payload, dict) else kb._json_dict(payload)
    return data if isinstance(data.get("wake_at"), (int, float)) else None


def park_state(conn: sqlite3.Connection, task_id: str) -> dict:
    """The park holding ``task_id`` in ``scheduled``, read one way for the dispatcher
    (:func:`pending_wake`, :func:`missing_wake`) and every display (``kanban_wake_summary``), so
    the shown wake cannot drift from the one acted on. [daveey]

    ``{"payload", "parked_at", "timed"}``: the latest ``SCHEDULED_ENTRY_EVENTS`` payload and its
    time; ``timed`` when that payload carries ``wake_at``. With no parking event the payload is
    empty and ``parked_at`` is the card's last event (else now)."""
    from hermes_cli import kanban_db as kb

    marks = ",".join("?" for _ in SCHEDULED_ENTRY_EVENTS)
    event = conn.execute(
        f"SELECT kind, payload, created_at, id, run_id FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (task_id, *SCHEDULED_ENTRY_EVENTS),
    ).fetchone()
    if event is None or _park_spent(conn, task_id, int(event[3]), event[4]):
        # No park, or only a spent one: a feed page can bring a remote run's ``scheduled`` row before the
        # ``continued`` event that carries its wake (t_01a1df6a505bf42f). [daveey]
        from hermes_cli.kanban_restore_chat_subs import restored_at
        restored = restored_at(conn, task_id)
        last = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ?", (task_id,)).fetchone()
        parked_at = max(int(last[0] or 0), restored or 0) if last else restored
        parked_at = parked_at or int(time.time())
        return {"payload": {}, "parked_at": parked_at, "timed": False, "event_id": None,
                "superseded": event is not None or restored is not None}
    timed = pending_wake_payload(event[0], event[1])
    return {"payload": timed if timed is not None else kb._json_dict(event[1]),
            "parked_at": int(event[2]), "timed": timed is not None, "event_id": int(event[3]),
            "superseded": False}


# A ``scheduled`` card whose only parking event is spent waits this long after its last event for the park that
# put it there to be imported, then wakes (a lost park event must not strand it). Feed pages land seconds apart.
SUPERSEDED_PARK_GRACE_SECONDS = 3600


def _park_spent(conn: sqlite3.Connection, task_id: str, event_id: int, run_id: Optional[int]) -> bool:
    """True when the parking event ``event_id`` already ended: its wake fired here, or a later run claimed the
    card. Such a park never holds the card again; a ``scheduled`` row after it belongs to a newer park whose event
    has not reached this board yet. The parking run's own claim, imported after its park, does not count, the
    rule ``kanban_wake_check.restore_missing_checks`` applies to the probe. [daveey]"""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND (kind = ? OR (kind IN ('claimed', 'spawned') "
        "AND (run_id IS NULL OR ? IS NULL OR run_id != ?))) LIMIT 1",
        (task_id, event_id, CONTINUATION_WOKEN_EVENT, run_id, run_id),
    ).fetchone() is not None


def pending_wake(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """``continued`` payload (with ``wake_at``) of the event that parked ``task_id`` in ``scheduled``."""
    park = park_state(conn, task_id)
    return park["payload"] if park["timed"] else None


def wake_due_continuations(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[str]:
    """Promote ``scheduled`` cards parked by a ``continued`` wait whose wake time has passed.

    The parking event is the latest ``SCHEDULED_ENTRY_EVENTS`` row, so a comment or heartbeat
    landing after it does not hide the wake. Cards scheduled by ``kanban_schedule`` (no wake
    time) are left for a human/cron unblock. Counters are untouched (``unblock_task`` would
    reset ``consecutive_failures``).
    """
    from hermes_cli.kanban_wake_check import own_gateway, run_due_wake_checks

    run_due_wake_checks(conn)  # condition wakes: start due probes off-thread, never waits [daveey]
    from hermes_cli.kanban_capability_registry import maybe_refresh_async
    maybe_refresh_async()  # live-check this gateway's capabilities off-thread, throttled [daveey]
    from hermes_cli.kanban_sweep_gate import gated
    # [daveey] G2: the park scan reads only content rows (the parking events, statuses), so it reruns when they
    # move or when the earliest wake it found arrives; condition probes above keep their own next_at clock.
    from hermes_cli import kanban_passive_intake as passive
    policy = passive.snapshot()
    return gated("dispatch.continuation_parks", conn, _wake_parks, conn, now, policy,
                 skipped=([], None), now=now,
                 extra=lambda db: (own_gateway(db), tuple(sorted(policy.items()))), run_due=lambda out: out[1])[0]


def _wake_parks(conn: sqlite3.Connection, now: Optional[int], passive_policy=None) -> tuple[list[str], Optional[int]]:
    """Wake the due parks; returns ``(woken, earliest wake still pending)`` (None when no park can come due)."""
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    woken: list[str] = []
    pending: list[int] = []
    from hermes_cli.kanban_wake_check import foreign_ids
    copies = foreign_ids(conn)  # [daveey] a worker node's copy: its home board owns the clock wake
    for row in conn.execute("SELECT id, block_kind FROM tasks WHERE status = 'scheduled'").fetchall():
        tid = row[0]
        if tid in copies:
            continue
        park = park_state(conn, tid)
        payload = park["payload"] if park["timed"] else None
        if payload is None:
            # Invariant: every scheduled card has a wake or becomes blocked. [daveey]
            if not _enforce_wake_invariant(conn, tid, row[1], now, woken):
                info = missing_wake(conn, tid)
                pending.append(int(info["wake_at"]) if info and info.get("wake_at") is not None else now)
            continue
        from hermes_cli import kanban_passive_intake as passive
        quiet_until = passive.quiet_until(conn, tid, park, now, policy=passive_policy)
        if quiet_until is not None:
            pending.append(quiet_until)
            continue  # recheck custody even if no board content changes
        wake_at = payload["wake_at"]
        pending.append(int(wake_at))  # a skipped or stale wake stays due: the next tick retries it
        if wake_at > now:
            continue
        resume = payload.get("resume_status") if payload.get("resume_status") in ("ready", "review") else "ready"
        with kb.write_txn(conn):
            from hermes_cli.kanban_terminal_guard import skip_wake
            if park_state(conn, tid).get("event_id") != park.get("event_id"):
                continue  # the park moved or was spent since the read above: the next tick reads it again
            if skip_wake(conn, tid, CONTINUATION_WOKEN_EVENT, wake_at=wake_at):
                continue  # a stale copy: the card completed after it parked (t_01a1c7b27ca3b496) [daveey]
            landing = kb._landing_status_after_parents(conn, tid)
            new_status = "review" if landing == "ready" and resume == "review" else landing
            cur = conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ? AND status = 'scheduled'",
                (new_status, tid),
            )
            if cur.rowcount == 1:
                kb._append_event(conn, tid, CONTINUATION_WOKEN_EVENT,
                                 {"status": new_status, "wake_at": wake_at})
                woken.append(tid)
    return woken, min(pending, default=None)


HUMAN_BLOCK_KINDS = ("needs_input", "capability")


def missing_wake(conn: sqlite3.Connection, task_id: str, *, park: Optional[dict] = None) -> Optional[dict]:
    """For a ``scheduled`` card with no timed wake: ``{"parked_at", "wake_at", "reason"}`` with the
    wake implied by ``kanban_timed_park.implied_wake`` (None when the card has a real wake).
    ``park`` is a :func:`park_state` the caller already read. [daveey]"""
    from hermes_cli.kanban_timed_park import implied_wake

    park = park_state(conn, task_id) if park is None else park
    if park["timed"]:
        return None
    if park.get("superseded"):  # its own park is still on the way: a late backstop, never the spent one's wake
        return {"parked_at": park["parked_at"], "wake_at": park["parked_at"] + SUPERSEDED_PARK_GRACE_SECONDS,
                "reason": None, "park_missing": True}
    payload, parked_at = park["payload"], park["parked_at"]
    reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
    return {"parked_at": parked_at, "wake_at": implied_wake(payload, parked_at), "reason": reason}


def blocks_for_human(park: dict, block_kind: Optional[str]) -> bool:
    """True when :func:`_enforce_wake_invariant` moves this park to ``blocked`` instead of waking
    it: no timed wake and a human ``block_kind``. Displays read this, not their own rule. A card whose park is
    still being imported is not judged by a block kind left from before it ran. [daveey]"""
    return not park["timed"] and not park.get("superseded") and block_kind in HUMAN_BLOCK_KINDS


def _enforce_wake_invariant(conn, tid: str, block_kind: Optional[str], now: int, woken: list) -> bool:
    """A scheduled card without a wake (parked by older code, or by ``kanban_schedule`` before
    every park carried one): a human-kind card goes to ``blocked`` at once -- it waits on a
    person, and ``scheduled`` hid it from every needs-me surface (t_6feb6afd, 2+ days) --
    anything else wakes at its implied time. True when the card moved. [daveey]"""
    from hermes_cli import kanban_db as kb

    park = park_state(conn, tid)
    info = missing_wake(conn, tid, park=park)
    if info is None:
        return False
    if blocks_for_human(park, block_kind):
        from hermes_cli.kanban_block_routing import latest_block_reason

        reason = latest_block_reason(conn, tid) or info["reason"] or (
            "Parked in scheduled with no wake while waiting on a human answer.")
        with kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'blocked', current_run_id = NULL WHERE id = ? AND status = 'scheduled'",
                (tid,))
            if cur.rowcount == 1:
                kb._append_event(conn, tid, "blocked", {
                    "reason": reason, "kind": block_kind, "source_status": "scheduled",
                    "wake_invariant": "human wait parked without a wake"})
        return cur.rowcount == 1
    if info["wake_at"] > now:
        return False
    with kb.write_txn(conn):
        from hermes_cli.kanban_terminal_guard import skip_wake
        again = park_state(conn, tid)
        if (again.get("event_id"), again.get("superseded")) != (park.get("event_id"), park.get("superseded")):
            return False  # a park arrived (or was spent) since the read above: the next tick reads it
        if skip_wake(conn, tid, CONTINUATION_WOKEN_EVENT, wake_at=info["wake_at"]):
            return False
        landing = kb._landing_status_after_parents(conn, tid)
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ? AND status = 'scheduled'",
            (landing, tid))
        if cur.rowcount == 1:
            extra = {"park_missing": True} if info.get("park_missing") else {}
            kb._append_event(conn, tid, CONTINUATION_WOKEN_EVENT, {
                "status": landing, "wake_at": info["wake_at"], "implied_wake": True, **extra})
            woken.append(tid)
    return cur.rowcount == 1


def render_checkpoint_context(conn: sqlite3.Connection, task_id: str) -> str:
    """The newest ``continued`` checkpoint, if no run has completed since. Placed near the top
    of the worker context so the next run resumes instead of re-orienting from scratch."""
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT id, outcome, metadata, summary FROM task_runs WHERE task_id = ? "
        "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 20", (task_id,),
    ).fetchall()
    checkpoint_row = None
    later: list[str] = []
    for row in rows:
        if row["outcome"] == CONTINUED_OUTCOME:
            checkpoint_row = row
            break
        if row["outcome"] in ("completed", "review_requested", "changes_requested"):
            return ""
        later.append(f"run {row['id']}: {row['outcome']}")
    if checkpoint_row is None:
        return ""
    meta = kb._json_dict(checkpoint_row["metadata"])
    checkpoint = meta.get("checkpoint") or checkpoint_row["summary"] or ""
    n = meta.get("continuation")
    lines = [
        f"## Continuation checkpoint (from run {checkpoint_row['id']}"
        + (f", continuation #{n}" if n else "") + ")",
        "The previous run of this task hit its runtime budget (or handed off a wait) and wrote this "
        "checkpoint on purpose. Resume from it: verify the listed state (git log/status, files, "
        "`kill -0 <pid>` and log tails for background jobs), re-adopt anything still "
        "running, and continue with the next steps. Do not restart finished steps.",
    ]
    if later:
        lines.append("Runs after the checkpoint (did not checkpoint again): " + ", ".join(reversed(later)) + ".")
    lines += ["", checkpoint.strip(), ""]
    return "\n".join(lines) + "\n"


def soft_deadline_message(*, elapsed: int, limit: int, final: bool) -> str:
    remaining = max(0, limit - elapsed)
    head = ("FINAL WARNING — the dispatcher hard-kills this run in about "
            f"{_fmt(remaining)}." if final else
            f"This run has used {_fmt(elapsed)} of its {_fmt(limit)} max runtime; the dispatcher "
            f"hard-kills it in about {_fmt(remaining)}.")
    return (
        f"[Kanban soft deadline] {head} If the task will not be fully finished (complete / "
        "request_review) within the next few minutes, stop starting new work and call "
        "`kanban_continue(checkpoint=...)` now. The checkpoint must let a fresh run resume without "
        "re-deriving anything: what is done (commits, branches, files, deploys), current state, the "
        "exact next steps, background processes to re-adopt (PIDs and log paths — the next run is a "
        "new process, so process_manage ids do not carry over; jobs must be persist_on_release or "
        "nohup to survive), worktrees, and what must not be redone. If you are only waiting on CI, a "
        "release, a soak or a training checkpoint, pass `resume_after_seconds` instead of polling. A "
        "continuation re-queues the card with your checkpoint and transcript; it is not a failure "
        "or a block. After kanban_continue returns, end your turn with a one-line reply."
    )


def _fmt(seconds: int) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m" + (f"{seconds % 60:02d}s" if seconds % 60 else "")
    return f"{seconds}s"
