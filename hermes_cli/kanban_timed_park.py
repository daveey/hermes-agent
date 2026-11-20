"""Timed parks: a block that waits on a clock or an outside event, not a person.

Workers park cards that wait on time (a soak, a nightly run, a release window, a
cron that re-checks) with ``kanban_block``, because goal-mode cards may not call
``kanban_schedule``. ``block_task`` used to re-kind a parent-less ``dependency``
wait to ``needs_input``, so the card sat in ``blocked`` and the Desktop sidebar
painted its chat red as "needs a human" although nobody had anything to do.

``block_task`` now routes such a wait here instead: the card lands in
``scheduled`` (not dispatchable, counted as in-progress, never red) with a wake
time on its ``scheduled`` event, which ``wake_due_continuations`` honours like a
``kanban_continue`` wait. A ``hermes kanban unblock`` (a wake cron) still wakes it
earlier. A timed park is one of:

* ``kind='time'``;
* ``kind='dependency'`` with no open parent (the wait is on something off the board);
* any kind whose reason carries ``SCHEDULED_UNTIL=<ISO8601>``.

Without an explicit time the card wakes after ``DEFAULT_WAKE_SECONDS`` so a park
can never sit forgotten: the next run re-checks and parks again. [daveey]
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

TIME_KIND = "time"
DEFAULT_WAKE_SECONDS = 24 * 3600
# An explicit SCHEDULED_UNTIL may park longer than a continuation wait, but not forever.
MAX_WAKE_SECONDS = 14 * 24 * 3600
MIN_WAKE_SECONDS = 60
_MARKER = re.compile(r"SCHEDULED_UNTIL=(\S+)")


def scheduled_until(reason: Optional[str], *, now: Optional[int] = None) -> Optional[int]:
    """Epoch seconds named by a ``SCHEDULED_UNTIL=<ISO8601>`` marker, clamped to
    ``[now + MIN_WAKE_SECONDS, now + MAX_WAKE_SECONDS]``; None when absent or unparseable.
    A naive timestamp is local time."""
    match = _MARKER.search(reason or "")
    if not match:
        return None
    raw = match.group(1).rstrip(".,;:)]}'\"")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    now = int(time.time()) if now is None else int(now)
    epoch = int(when.timestamp())
    return max(now + MIN_WAKE_SECONDS, min(epoch, now + MAX_WAKE_SECONDS))


def scheduled_payload(reason: Optional[str], *, now: Optional[int] = None) -> dict:
    """``scheduled`` event payload for ``kanban_schedule``. Every scheduled card carries a wake:
    a ``SCHEDULED_UNTIL=`` marker sets it, otherwise ``DEFAULT_WAKE_SECONDS``. A park without
    one waited on a cron/human that may never come (a deleted cron left t_2ba76784 parked for
    days); a cron or ``unblock`` still wakes the card earlier. [daveey]"""
    now = int(time.time()) if now is None else int(now)
    wake_at = scheduled_until(reason, now=now) or now + DEFAULT_WAKE_SECONDS
    return {"reason": reason, "wake_at": wake_at}


def implied_wake(payload: dict, event_at: int) -> int:
    """Wake time for a parking event written before every park carried ``wake_at``: the
    ``SCHEDULED_UNTIL=`` marker in its reason, else ``DEFAULT_WAKE_SECONDS`` after the park.
    The marker is clamped against the park time, so an overdue legacy wake is due now. [daveey]"""
    wake = payload.get("wake_at")
    if isinstance(wake, (int, float)):
        return int(wake)
    reason = payload.get("reason")
    return scheduled_until(reason if isinstance(reason, str) else None, now=event_at) or event_at + DEFAULT_WAKE_SECONDS


def blocker_wake_seconds(cfg: Optional[dict] = None) -> int:
    """Wake delay for a shared-blocker park that named no wake: the shared probe interval,
    clamped to ``[MIN_WAKE_SECONDS, kanban_wake_check.GUARD_SECONDS]``. The woken card goes
    ``ready`` and ``shared_blocker_guard`` lets one card per interval probe, so a board whose
    every card is parked behind the blocker still probes it. [daveey]"""
    from hermes_cli.kanban_shared_blockers import probe_interval_seconds
    from hermes_cli.kanban_wake_check import GUARD_SECONDS

    return max(MIN_WAKE_SECONDS, min(probe_interval_seconds(cfg), GUARD_SECONDS))


def needs_blocker_wake(kind: Optional[str], *, blocker_id: Optional[str], human_request,
                       open_parent: bool) -> bool:
    """A blocker park with no other way back: no human ask, no open parent to wait on, and not
    an ``assignment`` (routed to a supervisor's repair card). Such a park used to land plain
    ``blocked`` and wait for the blocker to clear, which nothing probed. [daveey]"""
    return bool(blocker_id) and not human_request and not open_parent and kind != "assignment"


def park_mode(kind: Optional[str], reason: Optional[str], *, parents_satisfied: bool,
              blocker_id: Optional[str] = None, human_request=None, wake_check: Optional[str] = None,
              clock_only: bool = False) -> Optional[str]:
    """How ``block_task`` parks: ``"timed"`` (:func:`park` with the caller's wake), ``"blocker"``
    (:func:`park` with ``blocker_wake``) or None (a block / todo / triage via ``route_block``).

    A blocker never cancels a timed park: the 2026-10-28 cards passed SCHEDULED_UNTIL, a
    wake_check and a blocker_id, and the blocker made ``block_task`` drop the other two. A
    visible human ask behind a blocker stays a block. A blocker park that brought no timing of
    its own (no SCHEDULED_UNTIL, wake_check or clock_only) wakes on the probe cadence. [daveey]"""
    if blocker_id and human_request:
        return None
    has_timing = bool(_MARKER.search(reason or "") or wake_check or clock_only)
    if blocker_id and not has_timing:
        if needs_blocker_wake(kind, blocker_id=blocker_id, human_request=human_request,
                              open_parent=not parents_satisfied):
            return "blocker"
        return None
    if is_timed_park(kind, reason, parents_satisfied=parents_satisfied):
        return "timed"
    if blocker_id and not parents_satisfied:
        return None
    if blocker_id and kind != "assignment":
        # A blocker park that brought a wake_check / clock_only but no SCHEDULED_UNTIL: keep
        # that probe (a plain block would drop it) on the default wake.
        return "timed"
    return None


def is_timed_park(kind: Optional[str], reason: Optional[str], *, parents_satisfied: bool,
                  blocker_id: Optional[str] = None) -> bool:
    """Whether a ``block_task`` call is really a wait on time, not on a human.
    Behind a shared blocker (``blocker_id``) a parent-less ``dependency`` is a wait on that
    blocker, not on time (:func:`park_mode` gives it the blocker wake instead); ``kind='time'``
    and a ``SCHEDULED_UNTIL=`` marker still park with their wake. [daveey]"""
    if kind == TIME_KIND or (kind == "dependency" and parents_satisfied and not blocker_id):
        return True
    return _MARKER.search(reason or "") is not None


def park(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str], requested_kind: Optional[str],
    source_status: str = "ready", expected_run_id: Optional[int] = None, from_untyped_block: bool = False,
    wake_check: Optional[str] = None, check_every: Optional[int] = None, clock_only: bool = False,
    awaiting=None, blocker_id: Optional[str] = None, run_metadata: Optional[dict] = None,
    blocker_wake: bool = False, probe_host: Optional[str] = None,
    blocker_provider: Optional[str] = None, blocker_model: Optional[str] = None,
    blocker_lane: Optional[str] = None,
) -> bool:
    """``running``/``ready``/``triage`` -> ``scheduled`` with a wake. Caller holds the write txn.
    ``from_untyped_block``: an untyped ``blocked`` card with no live run (classify-in-place) instead.
    ``awaiting`` names the producer (``kanban_awaiting.gate``: refused or escalated otherwise).

    ``blocker_id`` (``kanban_shared_blockers``) lands on the closed run's metadata and on the
    event, as on a plain block, so the card stays behind the blocker while it waits on its own
    wake. ``blocker_wake``: the caller named a blocker and no wake at all. The card then wakes
    after :func:`blocker_wake_seconds`; its producer is the shared probe cadence, so the awaiting
    gate is not consulted. [daveey]"""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    from hermes_cli.kanban_blocker_metadata import blocker_metadata, FIELDS
    meta = blocker_metadata(conn, task_id, reason=reason, metadata=run_metadata,
                            blocker_id=blocker_id, blocker_provider=blocker_provider,
                            blocker_model=blocker_model, blocker_lane=blocker_lane)
    blocker_id = meta.get("blocker_id")
    now = int(time.time())
    if blocker_wake:
        wake_at = now + blocker_wake_seconds()
    else:
        wake_at = scheduled_until(reason, now=now) or now + DEFAULT_WAKE_SECONDS
    guard = ("status = 'blocked' AND block_kind IS NULL AND current_run_id IS NULL" if from_untyped_block
             else "status IN ('running', 'ready', 'triage')")
    if blocker_wake:
        record = {"kind": "clock", "inferred": True, "shared_blocker": blocker_id,
                  "why": f"shared blocker {blocker_id}: re-checked on the shared probe cadence"}
    else:
        try:
            record = kaw.gate(conn, task_id, awaiting=awaiting, reason=reason, wake_check=wake_check,
                              clock_only=clock_only, op="block", blocker_id=blocker_id)
        except kaw.RepeatedPark as rp:
            return kaw.escalate(conn, task_id, record=rp.record, reason=reason,
                                expected_run_id=expected_run_id) is not None
        from hermes_cli import kanban_deploy_window as kdw
        wake_at, reason = kdw.apply(record, wake_at, reason, now=now)
        reason, check_every = kaw.resource_apply(record, reason, check_every)
    wake_check, clock_only = kaw.park_probe(conn, task_id, record, wake_check, clock_only)
    wake_check, check_every = kwc.validate_park(
        wake_at=wake_at, wake_check=wake_check, check_every=check_every, clock_only=clock_only)
    from hermes_cli.kanban_wake_check_hub_relay import relay_host
    probe_host = probe_host or relay_host(conn, task_id, wake_check)  # [daveey] a hub card's board probe
    sql = ("UPDATE tasks SET status = 'scheduled', human_request = NULL, claim_lock = NULL, claim_expires = NULL, "
           "consecutive_failures = CASE WHEN status = 'triage' THEN 0 ELSE consecutive_failures END, "
           "last_failure_error = CASE WHEN status = 'triage' THEN NULL ELSE last_failure_error END, "
           f"worker_pid = NULL WHERE id = ? AND {guard}")
    params: tuple = (task_id,)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (task_id, int(expected_run_id))
    from hermes_cli.kanban_source_receipt import with_source_receipt
    if with_source_receipt(conn, task_id, lambda: conn.execute(sql, params).rowcount) != 1:
        return False
    run_id = kb._end_or_synthesize_run(
        conn, task_id, outcome="scheduled", status="scheduled", summary=reason, metadata=meta or None,
        synthesize=bool(reason or meta),
    )
    kb._append_event(conn, task_id, "scheduled", {
        "reason": reason, "timed_park": True, "requested_kind": requested_kind,
        "wake_at": wake_at, "resume_status": source_status,
        **kwc.park_fields(wake_check, check_every, clock_only, conn=conn, probe_host=probe_host), "awaiting": record,
        **({"blocker_id": blocker_id} if blocker_id else {}),
        **{key: meta[key] for key in FIELDS if key in meta},
        **({"blocker_wake": True} if blocker_wake else {}),
        **({"clear_blocker": True} if meta.get("clear_blocker") else {}),
    }, run_id=run_id)
    # [daveey] ``probe_host``: another gateway runs this probe (a relay to the home); it registers it there.
    kwc.set_check(conn, task_id, wake_check, check_every, now=now,
                  register_here=kwc.probe_here(conn, probe_host))
    from hermes_cli.kanban_done_tail import post_notice
    post_notice(conn, task_id, reason=reason, awaiting=record, run_id=run_id)
    from hermes_cli import kanban_decision_ask
    kanban_decision_ask.post_notice(conn, task_id, reason=reason, awaiting=record, run_id=run_id)
    return True
