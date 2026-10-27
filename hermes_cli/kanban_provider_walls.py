"""Provider quota walls: stop respawning kanban workers into a wall that lasts hours or days. [daveey]

2026-10-26: the hub spawned workers on openai-codex that died ~60 s later with ``retry after 524389s``
(~6 days), 285 times in 24h. A ``rate_limited`` exit is requeued as neutral, so nothing noticed.

* :func:`record_wall` runs when a worker exits rate-limited. It reads the retry-after from the worker log
  and stores ``provider -> until`` in ``<kanban home>/provider_walls.json`` (a wall under
  ``MIN_WALL_SECONDS`` is left to the normal respawn cooldown).
* :func:`retier_for_walls` runs at claim time, after the tier decision. If the provider the worker would
  run on is walled, the card moves to the configured fallback (``kanban.default_model``/``default_provider``)
  unless the fallback is walled too. This covers keep-pattern, review and retry tiers (which resolve to
  ``model: null`` = profile default) as well as explicit pins. A card whose body contains ``require_model``
  keeps its pin.
* :func:`rate_limit_storm` counts ``rate_limited`` runs in the last hour for the alert card.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIN_WALL_SECONDS = 15 * 60
DEFAULT_WALL_SECONDS = 30 * 60
STORM_THRESHOLD = 10
REQUIRE_MODEL_MARKER = "require_model"
_RETRY_RE = re.compile(r"retry[\s_-]*after[:=\s]+(\d+)\s*s", re.IGNORECASE)


def walls_path() -> Path:
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "provider_walls.json"


def _load(path: Optional[Path] = None) -> dict[str, float]:
    try:
        data = json.loads((path or walls_path()).read_text(encoding="utf-8-sig"))
        return {str(k): float(v) for k, v in data.items()}
    except Exception:
        return {}


def active_walls(*, now: Optional[float] = None, path: Optional[Path] = None) -> dict[str, float]:
    now = time.time() if now is None else now
    return {p: u for p, u in _load(path).items() if u > now}


def parse_retry_after(text: str) -> Optional[int]:
    """Largest ``retry after Ns`` in ``text`` (the log tail), or None."""
    vals = [int(m) for m in _RETRY_RE.findall(text or "")]
    return max(vals) if vals else None


def _norm(provider: Optional[str]) -> str:
    return str(provider or "").strip().lower()


def profile_provider() -> str:
    try:
        from hermes_cli.config import load_config
        model = (load_config() or {}).get("model") or {}
        return _norm(model.get("provider")) if isinstance(model, dict) else ""
    except Exception:
        return ""


def record_wall(provider: str, retry_after: Optional[int], *, now: Optional[float] = None,
                path: Optional[Path] = None) -> Optional[float]:
    """Mark ``provider`` unavailable for ``retry_after`` s. Returns the expiry, or None if too short to track."""
    provider = _norm(provider)
    if not provider or provider == "auto":
        return None
    now = time.time() if now is None else now
    seconds = DEFAULT_WALL_SECONDS if retry_after is None else int(retry_after)
    if seconds < MIN_WALL_SECONDS:
        return None
    until = now + seconds
    p = path or walls_path()
    walls = active_walls(now=now, path=p)
    walls[provider] = max(until, walls.get(provider, 0))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(walls), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("kanban walls: cannot write %s (%s)", p, exc)
    return walls[provider]


def record_wall_for_run(conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
                        log_path: Optional[Path]) -> Optional[float]:
    """Wall bookkeeping for a rate-limited exit: provider from the run's ``model_tier`` event."""
    provider = ""
    try:
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'model_tier' "
            "AND (run_id IS ? OR run_id IS NULL) ORDER BY id DESC LIMIT 1", (task_id, run_id)):
            provider = _norm((json.loads(row[0] or "{}")).get("provider"))
        if not provider:
            row = conn.execute("SELECT provider_override FROM tasks WHERE id = ?", (task_id,)).fetchone()
            provider = _norm(row[0]) if row else ""
    except Exception as exc:
        logger.debug("kanban walls: provider lookup failed for %s (%s)", task_id, exc)
    provider = provider or profile_provider()
    retry = None
    try:
        if log_path and log_path.exists():
            data = log_path.read_bytes()[-20000:].decode("utf-8", "replace")
            retry = parse_retry_after(data)
    except OSError:
        pass
    return record_wall(provider, retry)


def retier_for_walls(task: Any, cfg: dict[str, Any], *, now: Optional[float] = None,
                     path: Optional[Path] = None) -> Optional[tuple[str, str, str]]:
    """``(model, provider, walled_provider)`` to run on instead, or None to leave the task alone."""
    walls = active_walls(now=now, path=path)
    if not walls:
        return None
    if REQUIRE_MODEL_MARKER in f"{getattr(task, 'title', '') or ''}\n{getattr(task, 'body', '') or ''}":
        return None
    provider = _norm(getattr(task, "provider_override", None)) or (
        profile_provider() if not str(getattr(task, "model_override", "") or "").strip() else "")
    if provider not in walls:
        return None
    model = str(cfg.get("default_model") or "").strip()
    fb = _norm(cfg.get("default_provider"))
    if not model or not fb or fb == "auto" or fb in walls:
        return None
    return model, fb, provider


def rate_limit_storm(conn: sqlite3.Connection, *, now: Optional[float] = None) -> int:
    now = int(time.time() if now is None else now)
    return conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE outcome = 'rate_limited' AND ended_at > ?", (now - 3600,)
    ).fetchone()[0]


def alert_on_storm(conn: sqlite3.Connection, *, board: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """Open ONE card per host per day when more than STORM_THRESHOLD rate_limited runs landed in the last hour."""
    n = rate_limit_storm(conn, now=now)
    if n <= STORM_THRESHOLD:
        return None
    import socket
    from hermes_cli import kanban_db as _kb
    host = socket.gethostname().split(".")[0]
    day = time.strftime("%Y%m%d", time.localtime(now or time.time()))
    walls = ", ".join(f"{p} until {time.strftime('%H:%M', time.localtime(u))}" for p, u in active_walls(now=now).items())
    return _kb.create_task(
        conn, title=f"{host}: {n} rate_limited kanban runs in the last hour (provider quota wall)",
        body=(f"More than {STORM_THRESHOLD} workers exited on a provider quota wall in an hour on {host}. "
              f"Known walls: {walls or 'none recorded'}. Check `kanban.default_model`/`default_provider`, the "
              "provider's usage limit, and cards pinned to the walled provider (`require_model`)."),
        assignee="default", created_by="rate-limit-alert", priority=80, idempotency_key=f"rate_limit_storm:{host}:{day}",
        board=board,
    )
