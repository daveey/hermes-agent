"""Refuse a card move whose stated reason is ship custody: ``hermes-core-wt ship`` lands from every host. [daveey]

Why: on 2026-11-12 the PR1187 owner (t_01a1cce501c36450), approved and CI-green, moved itself to ``machine:hub``
with the reason "Final hermes-core-wt ship must run in genuine hub task custody" and then sat ready behind the hub's
full local lane for 45+ minutes (t_01a1cf350800e915). The advice it followed was stale: since the state ship gate
judges a moved card's runs and a review that ran elsewhere from the Board's complete execution record, ship lands
from any machine (PR1318 landed from hermes-worker-3 the same hour). A move only re-queues the ship behind another
host's backlog. The refusal names the remedy; a card that really needs one host (an unpushed worktree that exists
only there, hub-local evidence) says so in ``host_reason`` and moves.
"""
from __future__ import annotations

import re
from typing import Optional

SHIP_CUSTODY_REFUSED = "ship_custody_portable"

# "ship ... custody", "custody ... land", "hub (task) custody": a claim that landing needs a particular host.
_CUSTODY = re.compile(
    r"\b(?:ship|shipping|land|landing)\b[^.;\n]{0,80}?\bcustody\b"
    r"|\bcustody\b[^.;\n]{0,80}?\b(?:ship|shipping|land|landing)\b"
    r"|\bhub\s+(?:task\s+)?custody\b",
    re.IGNORECASE,
)


def cites_ship_custody(reason: Optional[str]) -> bool:
    """True when a move reason claims the ship/land step needs a particular host's custody."""
    return bool(_CUSTODY.search(reason or ""))


def refusal(reason: Optional[str], host_reason: Optional[str]) -> Optional[str]:
    """The refusal message for a ship-custody move without ``host_reason``, else None (the move may proceed)."""
    if (host_reason or "").strip() or not cites_ship_custody(reason):
        return None
    return ("hermes-core-wt ship lands from every machine: its gate reads runs and reviews that happened elsewhere "
            "from the Board's execution record, so moving for ship custody only queues the ship behind another "
            "host's backlog. Run ship here; if it refuses, follow the remedy it prints. If this card really needs "
            "that host (e.g. an unpushed worktree that exists only there), pass host_reason naming it.")
