"""Own-hold decision and provider upkeep before dispatcher capacity admission. [daveey]"""
from hermes_cli import kanban_db as kb
from hermes_cli import loop_accounting as la


def run(conn, result, *, board, dry_run):
    from hermes_cli.kanban_idle_candidates import sweep_shared_blockers
    la.timed('dispatch.sweep_shared_blockers', sweep_shared_blockers, conn, result)
    if not dry_run:
        from hermes_cli.kanban_hold_notices import sweep as holds
        la.timed('dispatch.hold_notices', holds, conn)
        from hermes_cli.kanban_parent_held import sweep as parents
        la.timed('dispatch.parent_held', parents, conn)
        from hermes_cli.kanban_scope_renewal_alarm import sweep as renewal_alarm
        la.timed('dispatch.scope_renewal_alarm', renewal_alarm, conn)
    from hermes_cli.kanban_provider_walls import tick_upkeep
    la.timed('dispatch.tick_upkeep', tick_upkeep, conn, board=board, dry_run=dry_run)
    from hermes_cli import kanban_db_dispatch_quota as quota
    try:
        result.quota_block_released = la.timed('dispatch.release_recovered_quota_blocks', quota.release_recovered_quota_blocks, conn)
    except Exception:  # health: allow BLE001 -- retain existing best-effort provider maintenance
        kb._log.warning('kanban: releasing recovered quota-wall blocks failed', exc_info=True)
