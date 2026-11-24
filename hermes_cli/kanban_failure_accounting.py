"""Atomic run-scoped failure accounting for local and Board-backed dispatch."""
import sqlite3
from typing import Optional


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int | None = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    infrastructure: bool = False, block_kind: str = "transient", run_id: int | None = None,
    allow_nested: bool = False, diagnostic: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).

    ``infrastructure=True``: the host refused the spawn (no restart-safe scope,
    #114720) — nothing about the card ran, so the run and event are recorded
    with ``infrastructure: true`` but ``consecutive_failures`` is left alone and
    the breaker never trips; the card stays retryable and
    :func:`check_respawn_guard` spaces the retries.
    """
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_failure_summary as summary
    import json
    if outcome == "spawn_failed":  # the governor snapshot: this tick tried the card and its spawn failed [daveey]
        from hermes_cli.kanban_governors import charge
        charge(task_id, "spawn_failed", detail={"reason": str(error)[:160], "infrastructure": infrastructure})

    evidence = {"error": error, **(event_payload_extra or {}), **(diagnostic or {})}
    refusal = not infrastructure and summary.classify_failure(outcome, evidence) == "provider_refusal"
    if refusal:
        force_trip, block_kind = True, "capability"
    _kb = dispatch._kb
    if failure_limit is None:
        failure_limit = dispatch.DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn, allow_nested=allow_nested):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        if refusal:
            captured_run = run_id if run_id is not None else row["current_run_id"]
            route_row = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                "AND kind IN ('run_route','model_tier') ORDER BY id DESC LIMIT 1",
                (task_id, captured_run),
            ).fetchone()
            route = json.loads(route_row[0]) if route_row else {}
            event_payload_extra = {
                **(event_payload_extra or {}),
                "worker_output": str(evidence.get("worker_output") or evidence.get("error") or "")[:2000],
                "run_number": captured_run, "provider": route.get("provider"), "model": route.get("model"),
                "reason_code": "provider_refusal",
            }
        # ``diagnostic`` (the classifier's event payload) is the local outcome event's
        # evidence; on a Board-primary card it rides the release to that event.
        from hermes_cli.kanban_failure_release import queue
        if queue(conn, task_id, run_id if run_id is not None else row["current_run_id"],
                 outcome=outcome, error=error, failure_limit=failure_limit, force_trip=force_trip,
                 infrastructure=infrastructure, block_kind=block_kind, extra=event_payload_extra or {},
                 **({"diagnostic": diagnostic} if diagnostic else {})):
            return False  # Only the authoritative feed can report a completed breaker transition.
        from hermes_cli import kanban_local_failure as local_failure
        run_id = run_id if run_id is not None else row["current_run_id"]
        if not local_failure.admit(conn, task_id, run_id):
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        if infrastructure or not (force_trip or failures >= effective_limit):
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                detail = {"failures": failures, "retry_status": retry_status}
                if infrastructure:
                    detail["infrastructure"] = True
                run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error, metadata=detail,
                )
                _kb._append_event(conn, task_id, outcome, {
                    "error": error, **detail, **({"failure_notice": "canonical"} if run_id is not None else {}),
                }, run_id=run_id)
            local_failure.finalized(conn, task_id, run_id, failures=failures,
                                    effective_limit=effective_limit, tripped=False,
                                    trigger_outcome=outcome, error=error, retry_status=retry_status,
                                    infrastructure=infrastructure)
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did.
        conn.execute(
            "UPDATE tasks SET status = 'triage', block_kind = ?, human_request = NULL, "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
               if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')",
            (block_kind, failures, error, task_id),
        )
        payload = {
            "kind": block_kind,
            "reason_code": "provider_refusal" if refusal else "worker_failures_exhausted",
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
        }
        if end_run:
            # Only the spawn path has an open run to close.
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata={
                    "failures": failures,
                    "trigger_outcome": outcome,
                    "effective_limit": effective_limit,
                    "limit_source": limit_source,
                    "retry_status": retry_status,
                },
            )
        if force_trip:
            # The caller applied its own bounded policy, so the counter cannot
            # judge this block: ``recompute_ready`` holds it for an operator.
            payload["sticky"] = True
        if event_payload_extra:
            payload = {**event_payload_extra, **payload}
        if fix_id := payload.get("fix_card"):
            from hermes_cli import kanban_systemic_crash as systemic
            if not systemic.attach(conn, fix_id, task_id, allow_nested=True):
                # Custody is settled before publishing the one final failure event.
                payload.update(sticky=True, fix_card_link_failed=True)
        if run_id is not None:
            payload["failure_notice"] = "canonical"
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        local_failure.finalized(conn, task_id, run_id, **{**payload, "tripped": True})
        return True
