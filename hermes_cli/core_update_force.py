"""[daveey] "Force update" request: push a pending core update through now, without waiting on running turns.

Desktop's version pill offers it (``POST /api/hermes/update/force``). The request is one JSON file beside the
drain's pause request, ``<root>/core-update/force-request``::

    {"version": 1, "requested_at": <epoch s>, "expires_at": <requested_at + FORCE_TTL_S>, "by": "<principal>"}

hermes-sync reads it directly (no core call), so the format is the contract: keep it backward compatible. While
it serves a force request its drainer stamps ``"force": true`` on the pause request, and every turn then stops at
its next step boundary, including turns the next backend would not auto-continue
(``hermes_cli.admission_fence.pause_forced``, ``tui_gateway.update_pause``). hermes-sync deletes the file after a
successful apply or once it has expired; a missing, unparseable or expired file is no request.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

FORCE_REQUEST = "force-request"
FORCE_TTL_S = 1800.0


def _path(root: Path | str) -> Path:
    return Path(root) / "core-update" / FORCE_REQUEST


def write_force_request(root: Path | str, by: str) -> dict:
    """Write (or refresh) the force request atomically; returns its body."""
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    body = {"version": 1, "requested_at": now, "expires_at": now + FORCE_TTL_S, "by": str(by)}
    tmp = path.with_name(f".{FORCE_REQUEST}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return body


def read_force_request(root: Path | str, now: Optional[float] = None) -> Optional[dict]:
    """The live force request, or None when there is none, it is unreadable or malformed, or it has expired."""
    try:
        body: Any = json.loads(_path(root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    try:
        expires_at = float(body["expires_at"])
        requested_at = float(body["requested_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if expires_at <= (time.time() if now is None else now):
        return None
    return {"version": body.get("version"), "requested_at": requested_at, "expires_at": expires_at,
            "by": str(body.get("by") or "")}


def running_turn_count(home: Path | str, now: Optional[float] = None) -> int:
    """Turns holding a live cross-process turn lease in ``home``'s state.db (read-only); 0 when unreadable."""
    from hermes_cli.sqlite_safe_read import connect_tracked

    db_path = Path(home) / "state.db"
    if not db_path.exists():
        return 0
    try:
        conn = connect_tracked(f"{db_path.resolve().as_uri()}?mode=ro", tracking_path=db_path, uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM session_turn_leases WHERE expires_at > ?",
                               (time.time() if now is None else now,)).fetchone()
        finally:
            conn.close()
    except Exception:
        logger.warning("core-update force: could not count running turns in %s", db_path, exc_info=True)
        return 0
    return int(row[0] or 0) if row else 0
