"""Hold new Kanban claims while the owning gateway drains for a restart."""
from pathlib import Path


def claim_if_admitting(conn, task_id, assignee, lane, ttl_seconds, admission_check=None):
    """Recheck the assignee's owner immediately before either ready/review claim. ``admission_check``
    runs inside the claim transaction (see ``claim_task``)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.profiles import resolve_profile_env
    try:
        owner_home = resolve_profile_env(assignee)
    except FileNotFoundError:
        owner_home = None
    paused = dispatch_paused(owner_home)
    held = core_update_hold() is not None
    if paused or held:  # hold may start mid-tick [daveey]
        from hermes_cli.kanban_db_sync import defer_claim, ORIGIN_LOCAL
        defer_claim(task_id, 'gateway_draining' if paused else 'update_admission_hold', ORIGIN_LOCAL)
        return None
    claim = kb.claim_review_task if lane == 'review' else kb.claim_task  # reassigned since the gates: CAS loses (#147534)
    if admission_check is None:
        return claim(conn, task_id, ttl_seconds=ttl_seconds, expect_assignee=assignee)
    return claim(conn, task_id, ttl_seconds=ttl_seconds, admission_check=admission_check, expect_assignee=assignee)


def core_update_hold():
    """The live hermes-sync core-update drain hold (``agent.core_update_drain``), or None.

    The gateway watcher already holds its own ticks; ``dispatch_once`` checks this too so ``hermes kanban
    dispatch`` and the dashboard nudge cannot claim inside a hold (hermes-hub 2026-11-11: a CLI dispatch
    claimed run 165540 inside one). A dead writer's hold does not count (``read_hold``). [daveey]
    """
    try:
        from agent.core_update_drain import active_hold
    except ImportError:
        return None
    return active_hold()


def dispatch_paused(home=None):
    """Read the owning gateway's lifecycle snapshot; dead snapshots must not park work forever."""
    from gateway.status import read_runtime_status, runtime_status_pid_is_live
    record = read_runtime_status(Path(home) / 'gateway_state.json' if home else None)
    return bool(record and (record.get('gateway_state') == 'draining' or record.get('restart_requested'))
                and runtime_status_pid_is_live(record))
