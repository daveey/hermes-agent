"""Owner maintenance before the spawn cap, including worker quota evidence."""


def sweep(conn, result, guard_notice, parent_unclaimed):
    from hermes_cli.kanban_block_policy import sweep_blocked_cards
    from hermes_cli.kanban_quota_detector import drain_owner
    from hermes_cli.kanban_llm_pacing_dispatch import refresh_pools
    refresh_pools(conn)
    sweep_blocked_cards(conn, result)
    guard_notice.sweep_remote_guard_holds(conn, result)
    drain_owner(conn)
    parent_unclaimed.safe_sweep(conn)  # A capped host is when a parent starves.
