"""Reserve the last already-authorized spending run for a park's own condition. [daveey]

This is subtractive admission, never a budget extension. Scope/safety holds still
win at the existing claim/terminal guards. A clock-only wait is unaffected. No
shell text is interpreted as either a terminal result or execution permission.
"""
from __future__ import annotations


def reserve(conn, task_id: str, kind: str) -> bool:
    """Defer generic wakes when a conditional park has exactly one budgeted run left."""
    if kind != 'continuation_woken':
        # Unstartable CI and broken/stale/lost probes must reach their repair path. They
        # cannot produce the terminal condition and must not be reserved forever.
        return False
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli import kanban_spend_decision as decision
    if decision.pending(conn, task_id):
        return False  # the existing hold, not this reservation, controls release
    from hermes_cli.kanban_continuation import park_state
    from hermes_cli.kanban_wake_check import foreign_ids, own_gateway
    if task_id in foreign_ids(conn):
        # A worker node's copy has that node's default limits, not the home board's authority: only the
        # canonical home reserves. Its generic wake replication is already gated by the home.
        return False
    park = park_state(conn, task_id)
    payload = park.get('payload') or {}
    # Probe rows are machine-local. A home card can carry an authenticated relayed park whose one live probe
    # is on the parking worker, not this DB. Only a TIMED relayed park is reservable: restore_missing_checks
    # repairs a lost remote probe only for parks with wake_at, so an implicit one could never be repaired.
    relayed = (park.get('timed') and not park.get('superseded') and isinstance(payload.get('wake_check'), str)
               and bool(payload['wake_check'].strip()) and isinstance(payload.get('probe_host'), str)
               and bool(payload['probe_host']) and payload['probe_host'] != own_gateway(conn))
    check = conn.execute("SELECT 1 FROM task_wake_checks WHERE task_id=? "
                         "AND purpose IS NOT 'ask_resolved'", (task_id,)).fetchone()
    if check is None and not relayed:
        return False
    limits = budget.card_limits(conn, task_id, budget.limits())
    spent = budget.spend(conn, task_id)
    if not limits.runs or limits.runs - spent.runs != 1 or budget.over(spent, limits):
        return False
    from hermes_cli import kanban_db as kb
    park_id = park.get('event_id')
    if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='terminal_run_reserved' "
                        "AND json_extract(payload,'$.park_event_id') IS ?", (task_id, park_id)).fetchone():
        kb._append_event(conn, task_id, 'terminal_run_reserved', {
            'park_event_id': park_id, 'deferred_kind': kind, 'remaining_runs': 1,
            'reason': 'Final authorized spending run reserved for the parked wake_check condition; '
                      'generic status and timer wakes do not consume it. No extra run or scope release granted.'})
    return True
