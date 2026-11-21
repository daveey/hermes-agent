"""Pickup alarm source exclusion, matching the Board's pickup_alarm.ts contract. [daveey]

The Board stamps alarm_source from the authenticated gateway and fences changes to
these detector identity fields. Legacy NULL falls back to created_on; an empty
source is an explicit refusal. Ownership may change without changing provenance.
No caller-supplied list of excluded machines is used.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

# The resource-admission observer shares the detector identity and pool producer.
_KEY = re.compile(r"(?:pickup-stall:([a-z0-9][a-z0-9-]{0,63}):[A-Za-z0-9._-]+|"
                  r"worker-resources-unknown:([a-z0-9][a-z0-9-]{0,63})):([1-9][0-9]*)")


RESERVED_PREFIXES = ("pickup-stall:", "worker-resources-unknown:")
DETECTOR = "pickup-stall-detector"


def reserved(key: Any) -> bool:
    """True for an idempotency key in the detector-only namespace (the Board fences it to the detector)."""
    return isinstance(key, str) and key.startswith(RESERVED_PREFIXES)


def refusal(card: Mapping[str, Any], machine: str) -> str | None:
    """Refuse the source or malformed detector-like rows; ordinary cards are unchanged."""
    key = card.get("idempotency_key")
    detector = card.get("created_by") == DETECTOR
    if not detector and not reserved(key):
        return None
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    source = (match[1] or match[2]) if match else None
    provenance = card.get("alarm_source")
    if provenance is None:
        provenance = card.get("created_on")
    if not detector or source is None or source != provenance:
        return "pickup_stall_provenance"
    return "pickup_stall_source" if source == machine else None
