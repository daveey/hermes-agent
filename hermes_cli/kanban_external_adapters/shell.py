"""Legacy shell probe -> watch poll. [daveey]

A park's ``wake_check`` is a shell probe: exit 0 = condition holds, 1 = not yet, 3 = unknown, anything else = the
probe is broken. That cannot say whether a CI run passed or failed, or whether GitHub is down. A probe may print
ONE line ``HERMES_WATCH {json}`` to give the full record (``job``, ``service``, ``detail``, ``url``,
``retry_after``, ``service_name``): the cheap upgrade path for any script. Unknown or malformed fields are
ignored, never trusted.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from hermes_cli.kanban_external_state import HEALTHS, JOBS, TERMINAL_JOBS

_LINE = re.compile(r"^\s*HERMES_WATCH\s+(\{.*\})\s*$")
DETAIL_MAX = 300


def parse_watch(out: str) -> Optional[dict]:
    """The last ``HERMES_WATCH {json}`` line of ``out`` that holds a JSON object, else None."""
    found = None
    for line in (out or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        try:
            value = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(value, dict):
            found = value
    return found


def _clean(watch: dict) -> dict:
    out: dict[str, Any] = {}
    if watch.get("job") in JOBS:
        out["job"] = watch["job"]
    if watch.get("service") in HEALTHS:
        out["service"] = watch["service"]
    for key, limit in (("detail", DETAIL_MAX), ("service_name", 60)):
        if isinstance(watch.get(key), str) and watch[key].strip():
            out[key] = watch[key].strip()[:limit]
    url = watch.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        out["url"] = url[:500]
    ra = watch.get("retry_after")
    if isinstance(ra, int) and not isinstance(ra, bool) and ra >= 0:
        out["retry_after"] = ra
    return out


def poll(rc: Any, out: str) -> dict:
    """One probe result as a watch poll. ``terminal`` is set when the probe says the job finished (exit 0, or a
    ``HERMES_WATCH`` line with a terminal job); ``outcome`` is then the job result or ``done_unknown``."""
    from hermes_cli.kanban_wake_check import output_error

    crashed = rc not in (0, 1, 3) or (rc == 1 and bool(output_error(out)))
    if crashed:
        why = output_error(out) or f"exit {rc}"
        return {"job": "unknown", "service": "probe_broken", "detail": f"probe failed: {why}"[:DETAIL_MAX],
                "url": None, "retry_after": None, "terminal": False, "outcome": None}
    base = {"job": "unknown", "service": "ok", "detail": "", "url": None, "retry_after": None, "terminal": False,
            "outcome": None}
    # Exit 1 means only "not yet"; progress requires an explicit HERMES_WATCH job.
    if rc == 3:
        base["detail"] = "probe could not tell (exit 3)"
    watch = _clean(parse_watch(out) or {})
    base.update({k: v for k, v in watch.items() if k != "service_name"})
    if "service_name" in watch:
        base["service_name"] = watch["service_name"]
    base["terminal"] = rc == 0 or base["job"] in TERMINAL_JOBS
    if base["terminal"]:
        base["outcome"] = base["job"] if base["job"] in TERMINAL_JOBS else "done_unknown"
    return base
