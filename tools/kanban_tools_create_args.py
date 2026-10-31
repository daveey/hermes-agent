"""Argument validators and constants for ``kanban_create``. [daveey]

Split out of tools/kanban_tools.py to keep that facade under its size cap.
"""
from __future__ import annotations

from typing import Any, Optional

MAX_RETRY_LIMIT = 10  # the widest retry breaker an agent may set
# Fields read back from a freshly created card into the kanban_create result.
CREATED_FIELDS = ("status", "workspace_kind", "workspace_path", "project_id", "human_request")


def max_retries_arg(args: dict) -> Optional[int]:
    """``kanban_create``'s ``max_retries``: None when omitted, else an int 1..MAX_RETRY_LIMIT (bools
    refused). One cap for create and the supervisor ``retry_limit`` op. Checked before placement, so
    an invalid value never reaches a remote create."""
    from tools.kanban_tools import _check  # lazy: kanban_tools imports this module

    value: Any = args.get("max_retries")
    if value is None:
        return None
    _check(isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_RETRY_LIMIT,
           f"max_retries must be an integer 1-{MAX_RETRY_LIMIT} (got {value!r}); omit it for the "
           "config/default breaker. Nothing changed.", rejection="invalid_args")
    return int(value)
