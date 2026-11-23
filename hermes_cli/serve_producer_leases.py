"""Pure same-boot deadlines; browser interest is never a transport retainer.

All times are absolute CLOCK_MONOTONIC values sampled at HTTP acceptance. A relay
transfers them unchanged; neither dequeue nor reconnect grants another lifetime.
These durations preserve Desktop renewal compatibility, not legacy dequeue delay.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

VISIBILITY_S = 75.0
WATCH_S = 900.0
RETAINER_S = 15.0


def _clock(now: float) -> None:
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('invalid monotonic clock')


@dataclass(frozen=True)
class BrowserInterest:
    boot_id: str
    accepted_at: float
    visibility_until: float
    watch_until: float

    @classmethod
    def accepted(cls, boot_id: str, *, now: float, visible: bool) -> BrowserInterest:
        _clock(now)
        return cls(boot_id, now, now + VISIBILITY_S if visible else now, now + WATCH_S)

    def remaining(self, boot_id: str, *, now: float) -> tuple[float, float]:
        _clock(now)
        if boot_id != self.boot_id or now < self.accepted_at:
            return 0.0, 0.0
        return max(0.0, self.visibility_until - now), max(0.0, self.watch_until - now)


class Retainer:
    """Expiry requests drain; it is NEVER proof the producer has terminated."""

    def __init__(self, boot_id: str, *, now: float):
        _clock(now)
        self.boot_id = boot_id
        self.renewed_at = now
        self.expires_at = now + RETAINER_S

    def renew(self, boot_id: str, *, now: float) -> bool:
        _clock(now)
        if not self.live(boot_id, now=now):
            return False
        self.renewed_at = now
        self.expires_at = now + RETAINER_S
        return True

    def live(self, boot_id: str, *, now: float) -> bool:
        _clock(now)
        return boot_id == self.boot_id and self.renewed_at <= now < self.expires_at
