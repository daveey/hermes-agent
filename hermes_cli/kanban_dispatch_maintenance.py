"""Owner maintenance before the spawn cap, including worker quota evidence."""
from hermes_cli.kanban_dispatch_storage import DispatchWriteContended


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
    _link_human_chats(conn)
    checkpoint()


def place_workers(conn, timed):
    """Isolate placement failures, but unwind the pass when its write budget expires."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_worker_placement import dispatch_tick
    try:
        timed("dispatch.worker_placement", dispatch_tick, conn)
    except DispatchWriteContended:
        raise
    except Exception:  # health: allow BLE001 -- ordinary placement failures retain local dispatch fallback
        kb._log.warning("kanban worker placement failed; continuing local dispatch", exc_info=True)


def _own_ownerless_cards(conn):
    """Hourly: every open card gets a live owning chat, including ones created while state.db was down."""
    import logging
    import time
    from hermes_cli.kanban_owner_chat import maybe_backfill
    try:
        maybe_backfill(conn, now=time.monotonic())
    except DispatchWriteContended:
        raise
    except Exception:  # health: allow BLE001 -- ownerless cards stay as they were; the next hour retries
        logging.getLogger(__name__).warning("kanban: owner-chat backfill failed", exc_info=True)


def _link_human_chats(conn):
    """Every card blocked on a human gets a chat the human reads (kanban_human_chat), on the home gateway."""
    import logging
    import time
    from hermes_cli.kanban_human_chat import maybe_sweep
    try:
        maybe_sweep(conn, now=time.monotonic())
    except DispatchWriteContended:
        raise
    except Exception:  # health: allow BLE001 -- the asks keep their links; the next tick retries
        logging.getLogger(__name__).warning("kanban: human-chat link pass failed", exc_info=True)


def _start_spend_reviews(conn):
    """Spend-stop reviews run outside the worker queue, so a full host never delays a budget decision."""
    import logging
    from hermes_cli.kanban_spend_reviewer import start_due
    from hermes_cli import kanban_allowance_maintenance
    try:
        kanban_allowance_maintenance.sweep(conn)
        start_due(conn)
    except DispatchWriteContended:
        raise
    except Exception:  # health: allow BLE001 -- held stops stay held; the next tick retries
        logging.getLogger(__name__).warning("kanban: starting spend reviews failed", exc_info=True)
