"""Owner maintenance before the spawn cap, including worker quota evidence."""


def sweep(conn, result, guard_notice, parent_unclaimed):
    from hermes_cli.kanban_block_policy import sweep_blocked_cards
    from hermes_cli.kanban_quota_detector import drain_owner
    sweep_blocked_cards(conn, result)
    guard_notice.sweep_remote_guard_holds(conn, result)
    drain_owner(conn)
    parent_unclaimed.safe_sweep(conn)  # A capped host is when a parent starves.
