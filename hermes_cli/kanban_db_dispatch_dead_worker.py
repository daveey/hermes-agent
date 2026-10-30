"""``_DeadWorker``: how the dead-worker sweep books one dead worker (moved out of kanban_db_dispatch). [daveey]"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hermes_cli.kanban_host_reboot import HOST_REBOOT_OUTCOME
from hermes_cli.kanban_owner_unavailable import OWNER_UNAVAILABLE_OUTCOME


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's credential/model — trips at once."""
    unknown_skill: bool = False  # died of ``Unknown skill(s)``: blocked as ``assignment`` at once [daveey]
    stdlib_shadow: bool = False  # traceback from a workspace ``<stdlib>.py``: blocked at once [daveey]
    host_reboot: bool = False  # the host rebooted under the worker: no failure counted [daveey]
    reboot_culprit: Optional[dict] = None  # this card's own sudo shutdown (journal line): blocked at once [daveey]
    owner_unavailable: bool = False  # the profile owner never answered the claim: no failure counted [daveey]
    owner_restart: bool = False  # the profile owner restarted under the attempt: requeued uncounted [daveey]

    @property
    def run_outcome(self) -> str:
        if self.host_reboot:
            return HOST_REBOOT_OUTCOME
        if self.owner_unavailable:
            return OWNER_UNAVAILABLE_OUTCOME
        if self.owner_restart:
            return "reclaimed"
        if self.event_kind in {"oom_killed", "signaled"}:
            return self.kind
        # A rate-limited requeue is recorded as ``rate_limited`` so board history
        # doesn't show a phantom crash for a quota wall.
        return "rate_limited" if self.rate_limited else "crashed"
