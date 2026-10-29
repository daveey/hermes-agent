"""Classify a dead Kanban worker's exit code. [daveey]

Split out of ``kanban_db_dispatch`` (code-health file cap).
"""

from __future__ import annotations


def exit_code_kind(code: int) -> tuple[str, int]:
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    from hermes_cli import kanban_db as kb

    if code == 0:
        return ("clean_exit", 0)
    if code == kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)
