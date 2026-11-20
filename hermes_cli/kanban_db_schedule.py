"""``schedule_task``: park a card in ``scheduled`` (split out of ``kanban_db``). [daveey]

Late-bound via ``_kb`` (import-cycle breaking) so monkeypatching ``kanban_db`` helpers in
tests still reaches this code. Re-exported from ``kanban_db``.
"""
from __future__ import annotations

import sqlite3
import time as _time
from typing import Any, Optional

from hermes_cli.kanban_workspace_publication import publication_guard


@publication_guard
def schedule_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    expected_run_id: Optional[int] = None, wake_check: Optional[str] = None,
    check_every: Optional[int] = None, clock_only: bool = False, awaiting: Any = None,
    clear_blocker: bool = False, comment: Optional[tuple[str, str]] = None,
    blocker_id: Optional[str] = None, blocker_provider: Optional[str] = None,
    blocker_model: Optional[str] = None, blocker_lane: Optional[str] = None, watch: bool = False,
) -> bool:
    """Park in ``scheduled`` (waiting on time, not a human; not dispatchable) until its wake
    (``SCHEDULED_UNTIL=`` in the reason, else ``kanban_timed_park.DEFAULT_WAKE_SECONDS``) or an
    ``unblock_task``. Refused (False) while the card waits on a human answer: ``scheduled`` is
    not on any needs-me surface, so such a park hid the ask (t_6feb6afd). [daveey]

    ``awaiting`` names the producer (``kanban_awaiting``); a park nothing would satisfy raises
    ``AwaitingError``, and a third park on one premise escalates to an owner card instead.

    ``comment`` (``(author, body)``) is written in the same txn and only when the park lands
    (``kanban_reason_comment``); ``human_answer_refusal`` explains the human-answer refusal.
    ``watch`` stamps the park a watch (``kanban_watch``): it does not hold its chat open while parked.
    Triage parks reset the dispatcher failure streak without an intermediate dispatchable
    status; block-loop recurrence evidence is retained, as on unblock."""
    from hermes_cli.kanban_reason_comment import reason_comment
    from hermes_cli import kanban_park_pin as kpp
    pin = kpp.plan(conn, task_id, reason)  # git reads stay outside the write txn
    with _kb.write_txn(conn), reason_comment(conn, task_id, comment) as noted:
        if _kb._awaiting_human_answer(conn, task_id):
            return False
        from hermes_cli.kanban_timed_park import scheduled_payload
        from hermes_cli import kanban_awaiting as kaw
        from hermes_cli import kanban_wake_check as kwc
        try:
            record = kaw.gate(conn, task_id, awaiting=awaiting, reason=reason, wake_check=wake_check,
                              clock_only=clock_only, op="schedule", blocker_id=blocker_id)
        except kaw.RepeatedPark as rp:
            return noted.keep(kaw.escalate(conn, task_id, record=rp.record, reason=reason,
                                           expected_run_id=expected_run_id) is not None)
        wake_check, clock_only = kaw.park_probe(conn, task_id, record, wake_check, clock_only)
        why = kaw.park_cycle_error(conn, task_id, record, wake_check)
        if why:
            raise kaw.AwaitingError(kaw.INVALID_AWAITING, why)
        from hermes_cli import kanban_deploy_window as kdw
        payload = scheduled_payload(reason)
        payload["wake_at"], reason = kdw.apply(record, payload["wake_at"], reason, now=int(_time.time()))
        reason, check_every = kaw.resource_apply(record, reason, check_every)
        payload["reason"] = reason
        task = _kb.get_task(conn, task_id)
        source_status = (_kb._retry_status_for_run(conn, task_id) if task and task.status == 'running'
                         else _kb._resume_status_from_events(conn, task_id))
        if source_status == 'review':
            payload['resume_status'] = source_status
        if watch:
            payload["watch"] = True
        wake_check, check_every = kwc.validate_park(
            wake_at=payload["wake_at"], wake_check=wake_check,
            check_every=check_every, clock_only=clock_only)
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   consecutive_failures = CASE WHEN status = 'triage' THEN 0 ELSE consecutive_failures END,
                   last_failure_error = CASE WHEN status = 'triage' THEN NULL ELSE last_failure_error END,
                   human_request = NULL,
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked', 'triage')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        from hermes_cli.kanban_source_receipt import with_source_receipt
        if with_source_receipt(conn, task_id, lambda: conn.execute(sql, params).rowcount) != 1:
            return False
        kpp.apply(conn, task_id, pin)  # unpushed state keeps a pooled card on this worker
        # A card behind a shared blocker stays behind it: a park without the id says nothing about
        # the wall (kanban_shared_blockers.run_effect) unless ``clear_blocker`` says it is gone.
        from hermes_cli.kanban_block_routing import clear_meta
        from hermes_cli.kanban_blocker_metadata import blocker_metadata
        meta = blocker_metadata(conn, task_id, reason=reason, metadata=clear_meta(clear_blocker),
                                blocker_id=blocker_id, blocker_provider=blocker_provider,
                                blocker_model=blocker_model, blocker_lane=blocker_lane)
        run_id = _kb._end_or_synthesize_run(
            conn, task_id, outcome="scheduled", status="scheduled", summary=reason,
            metadata=meta or None, synthesize=bool(reason or meta),
        )
        from hermes_cli.kanban_wake_check_hub_relay import relay_host
        host = relay_host(conn, task_id, wake_check)  # [daveey] a hub card's board probe runs on the hub
        _kb._append_event(conn, task_id, "scheduled", {
            **payload, **kwc.park_fields(wake_check, check_every, clock_only, conn=conn, probe_host=host),
            "awaiting": record, **meta,
        }, run_id=run_id)
        kwc.set_check(conn, task_id, wake_check, check_every, register_here=kwc.probe_here(conn, host))
        from hermes_cli.kanban_done_tail import post_notice
        post_notice(conn, task_id, reason=reason, watch=watch, awaiting=record, run_id=run_id)
        from hermes_cli import kanban_decision_ask
        kanban_decision_ask.post_notice(conn, task_id, reason=reason, awaiting=record, run_id=run_id)
        return noted.keep(True)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
