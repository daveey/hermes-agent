"""Owner maintenance before the spawn cap, including worker quota evidence."""


def sweep(conn, result, guard_notice, parent_unclaimed, *, checkpoint=lambda: None):
    from hermes_cli.kanban_block_policy import sweep_blocked_cards
    from hermes_cli.kanban_quota_detector import drain_owner
    from hermes_cli.kanban_llm_pacing_dispatch import refresh_pools
    refresh_pools(conn)
    checkpoint()
    sweep_blocked_cards(conn, result)
    checkpoint()
    guard_notice.sweep_remote_guard_holds(conn, result)
    checkpoint()
    drain_owner(conn)
    checkpoint()
    parent_unclaimed.safe_sweep(conn)  # A capped host is when a parent starves.
    checkpoint()
    _start_spend_reviews(conn)
    checkpoint()
    _own_ownerless_cards(conn)
    checkpoint()


def _own_ownerless_cards(conn):
    """Hourly: every open card gets a live owning chat, including ones created while state.db was down."""
    import logging
    import time
    from hermes_cli.kanban_owner_chat import maybe_backfill
    try:
        maybe_backfill(conn, now=time.monotonic())
    except Exception:  # health: allow BLE001 -- ownerless cards stay as they were; the next hour retries
        logging.getLogger(__name__).warning("kanban: owner-chat backfill failed", exc_info=True)


def _start_spend_reviews(conn):
    """Spend-stop reviews run outside the worker queue, so a full host never delays a budget decision."""
    import logging
    from hermes_cli.kanban_spend_reviewer import start_due
    try:
        start_due(conn)
    except Exception:  # health: allow BLE001 -- held stops stay held; the next tick retries
        logging.getLogger(__name__).warning("kanban: starting spend reviews failed", exc_info=True)
