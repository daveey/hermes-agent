"""A queued creation's executable wait handle and bounded fallback guidance."""
from __future__ import annotations

import shlex


def queued_wait(args, target, note):
    """Return a quoted argv probe, never a SQL literal that looks like a secret."""
    if key := args.get("idempotency_key"):
        return {"wake_check": shlex.join(["hermes", "kanban", "wait-check", "key", key, "--gateway", target]),
                "note": note + " To wait for this card, park with kanban_schedule using exactly this wake_check; "
                "never a constant probe (`sleep 1; exit 1`) or a clock_only park."}
    return {"note": note + " Park at most 2 h without a probe, then find the id with `hermes-hub requests`."}
