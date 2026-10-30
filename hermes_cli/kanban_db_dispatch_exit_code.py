"""Classify a dead Kanban worker's exit code. [daveey]

Split out of ``kanban_db_dispatch`` (code-health file cap).
"""

from __future__ import annotations

# Worker exit "the profile owner restarted under this attempt" (its restart wait outlasted its own cap,
# or the restart ended the turn): the host's restart, not the card's fault, so the sweep requeues it as
# ``reclaimed`` WITHOUT counting a failure (``kanban_worker_client.OwnerRestartLost``). Only for a
# run the owner ADMITTED; one it never admitted exits 69 (``kanban_owner_unavailable``). 76 == BSD
# EX_PROTOCOL, unused by any other worker exit. [daveey]
KANBAN_OWNER_RESTART_EXIT_CODE = 76


def exit_code_kind(code: int) -> tuple[str, int]:
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_owner_unavailable import KANBAN_OWNER_UNAVAILABLE_EXIT_CODE

    if code == 0:
        return ("clean_exit", 0)
    if code == kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    if code == KANBAN_OWNER_UNAVAILABLE_EXIT_CODE:
        return ("owner_unavailable", code)
    if code == KANBAN_OWNER_RESTART_EXIT_CODE:
        return ("owner_restart", code)
    return ("nonzero_exit", code)
