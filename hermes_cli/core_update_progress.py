"""What this host's background core update is doing right now, for Desktop's version pill. [daveey]

``hermes-sync`` updates core on its own schedule and publishes the batch it is in to
``<root>/cache/core-update/window.json`` (read through :mod:`hermes_cli.kanban_deploy_window`), plus
the live drainer's record ``drain.json`` and the deferral reasons ``governor-other-defers.json``. Desktop only
knew about updates it started itself, so a hub update that held a user's message for 16 minutes showed
nothing but "Waiting for a Hermes update" on the message (hermes-hub 2026-11-19).

:func:`read_progress` turns those records into one small payload. Elapsed times are computed here, on the
host's clock, so a client with a skewed clock still draws the right bar. Anything missing, stale, foreign
or malformed answers None ("no update in progress"), never an error: the pill must not break on it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

ACTIVE_STATES = ("batching", "draining", "applying", "blocked")
# governor-other-defers.json is rewritten on every deferred pass (about every 2 min while draining).
DEFERS_FRESH_S = 600
MAX_WAITING_ON = 8


def _core_update_dir() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / "cache" / "core-update"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _waiting_on(cu: Path, now: float) -> list[str]:
    data = _read_json(cu / "governor-other-defers.json") or {}
    ts = _number(data.get("ts"))
    reasons = data.get("reasons")
    if ts is None or now - ts > DEFERS_FRESH_S or ts > now + 60 or not isinstance(reasons, list):
        return []
    return [r for r in reasons if isinstance(r, str) and r][:MAX_WAITING_ON]


def read_progress(now: Optional[float] = None) -> Optional[dict]:
    """The background core update on this host, or None when none is owed or nothing usable is published.

    ``state``      batching (waiting for its batch window) | draining (pausing work before the swap)
                   | applying (``hermes update`` is running) | blocked (owed, but nothing can apply it)
    ``due_at``     Unix seconds the update became (or becomes) due; ``state_since`` when ``state`` began
    ``elapsed_s``  seconds since ``due_at`` on this host's clock (negative while batching: due in the future)
    ``cap_s``      the drainer's own apply-over-anything cap while draining, when it published one
    ``waiting_on`` what the latest deferral named (sessions, cron runs, holds), at most MAX_WAITING_ON
    """
    from hermes_cli.kanban_deploy_window import read_window

    now = time.time() if now is None else now
    window = read_window(int(now))
    if not window or window["state"] not in ACTIVE_STATES:
        return None
    cu = _core_update_dir()
    drain = _read_json(cu / "drain.json") or {}
    cap_s = _number(drain.get("total_cap_s")) if drain.get("state") in ("draining", "applying") else None
    return {
        "state": window["state"],
        "due_at": window["window_at"],
        "state_since": window["state_since"],
        "elapsed_s": round(now - window["window_at"], 1),
        "cap_s": cap_s,
        "waiting_on": _waiting_on(cu, now) if window["state"] in ("draining", "blocked") else [],
    }
