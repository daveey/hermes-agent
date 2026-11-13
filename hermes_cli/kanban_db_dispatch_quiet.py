"""Claim-quiet result fields shared by ``DispatchResult`` (kept out of the facade module)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QuietResultFields:
    """``quiet_deferred``: ``(task_id, episode_id)`` automatic native-local claims intentionally deferred by a
    root claim-quiet lease (``kanban_db_dispatch_admission.quiet_claim_check``): no claim, run, failure or
    timestamp change. ``quiet_deadlines``: episode_id -> lease ``expires_at`` (epoch seconds)."""

    quiet_deferred: list[tuple[str, str]] = field(default_factory=list)
    quiet_deadlines: dict[str, float] = field(default_factory=dict)
