"""Code provenance for durable failed-wake deadlines, not busy backoff."""
from __future__ import annotations

import math
from typing import Any


def booking(due: float, short_due: float) -> dict:
    """Keep the wall deadline on this code, but let a different build retry sooner.

    Host-loop identity is frozen before its code-skew watchdog can see an update;
    reading the checkout here would incorrectly attribute an old process to new code.
    """
    from hermes_cli.host_loop_lease import _core_sha
    sha = _core_sha()
    if sha and due > short_due:
        return {"retry_code_sha": sha, "retry_short_at": short_due}
    return {}


def effective_due(closeout: dict) -> Any:
    """Read-only eligibility; the existing close-out CAS still owns the actual wake.

    Legacy/unknown provenance keeps the original deadline. The short deadline is
    fixed at booking, so repeated discovery does not postpone it forever.
    """
    due = closeout.get("retry_at")
    sha = closeout.get("retry_code_sha")
    short = closeout.get("retry_short_at")
    if not isinstance(sha, str) or not sha:
        return due
    if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
               for t in (due, short)):
        return due
    from hermes_cli.host_loop_lease import _core_sha
    current = _core_sha()
    if current and current != sha:
        return min(due, short)
    return due
