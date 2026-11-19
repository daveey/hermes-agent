"""Runtime-only closing receipts; a late segment seal is not a clean epoch tail.

These lifecycle files do not change the v1 event envelope. The final timestamp is
the caller's publication authorization after segment seal IO, in the same process
monotonic clock as the deadline; it is not receipt-publication completion time.
Missing or invalid receipts are incomplete evidence, never a zero-loss assertion.
"""
from __future__ import annotations

import json

from hermes_cli.kanban_fairness_segments import encoded, read_bounded, write

OPEN = "runtime-open.json"
CLOSE = "runtime-close.json"
IDENTITY = {"capture_id", "board_uid", "source_gateway", "source_epoch", "clock_error_ns"}


def open_epoch(out, identity):
    write(out / OPEN, encoded({"version": 1, **identity}))


def close_epoch(out, identity, manifest, deadline_ns, completed_ns):
    write(out / CLOSE, encoded({"version": 1, **identity, "final_seal": manifest["final_seal"],
                               "deadline_ns": deadline_ns, "completed_ns": completed_ns}))


def validate_tail(out, manifest):
    """Return gaps for runtime directories; preserve existing nonruntime captures."""
    if out.parent.name != "runtime" and not (out / OPEN).exists():
        return []
    try:
        opening = json.loads(read_bounded(out / OPEN, 4096))
        closing = json.loads(read_bounded(out / CLOSE, 4096))
        if (not isinstance(opening, dict) or not isinstance(closing, dict)
                or set(opening) != IDENTITY | {"version"}
                or set(closing) != IDENTITY | {"version", "final_seal", "deadline_ns", "completed_ns"}
                or type(opening["version"]) is not int or opening["version"] != 1
                or type(closing["version"]) is not int or closing["version"] != 1):
            raise ValueError("runtime receipt shape")
        if any(opening[k] != closing[k] for k in IDENTITY):
            raise ValueError("runtime receipt identity")
        if any(closing[k] != manifest.get(k) for k in ("capture_id", "board_uid", "final_seal")):
            raise ValueError("runtime receipt seal")
        sources = manifest.get("sources")
        if (not isinstance(sources, list) or len(sources) != 1
                or any(sources[0].get(k) != closing[k] for k in ("source_gateway", "source_epoch"))):
            raise ValueError("runtime receipt source")
        if (any(type(closing[k]) is not int or closing[k] < 0 for k in ("completed_ns", "deadline_ns"))
                or closing["completed_ns"] > closing["deadline_ns"]):
            raise ValueError("runtime deadline exceeded")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return [{"code": "runtime_tail"}]
    return []
