"""Provider quota walls: stop respawning kanban workers into a wall that lasts hours or days. [daveey]

2026-10-26: the hub spawned workers on openai-codex that died ~60 s later with ``retry after 524389s``
(~6 days), 285 times in 24h. A ``rate_limited`` exit is requeued as neutral, so nothing noticed.

* :func:`record_wall` stores ``provider -> until`` in ``provider_walls.json``.
  Reaped attempts use their exact receipt/episode deadline; the log reader is
  retained only for older callers without an episode.
* :func:`retier_for_walls` moves ordinary default routing (``tier_reason: default``, no pin) off a walled
  provider outside a quota episode: ``kanban.default_model`` first, then the strong tier when the default
  provider is the walled one. A provider also counts as walled when its OAuth pool is quota-exhausted for
  the model right now (:func:`live_quota_wall`). Explicit pins and strong tiers are never rewritten; a card
  whose body has a ``require_model: true`` line keeps its route (:func:`requires_model`). Attempt-specific
  admission, the class-route alternate, backoff and notices live in ``kanban_quota_episode``.
* :func:`rate_limit_storm` counts this host's ``rate_limited`` runs in the last hour for the alert card.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MIN_WALL_SECONDS = 15 * 60
DEFAULT_WALL_SECONDS = 30 * 60
STORM_THRESHOLD = 10
# A deliberate body line, not a substring: the storm alert's own prose named ``require_model`` and so opted the
# alert card out of the wall reroute it was written about (2026-11-12, run 2805 spawned into the codex wall). [daveey]
REQUIRE_MODEL_MARKER = "require_model: true"
_REQUIRE_MODEL_RE = re.compile(r"^[ \t]*require_model[ \t]*:[ \t]*(?:true|yes|1)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_RETRY_RE = re.compile(r"retry[\s_-]*after[:=\s]+(\d+)\s*s", re.IGNORECASE)


def requires_model(task: Any) -> bool:
    """Historical body directives cannot override model-neutral routing policy."""
    return False


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
    from hermes_cli.kanban_quota_episode import _disproved, _unhinted, episode
    state = episode(conn, task_id)
    if state and state['run_id'] == run_id:
        if state.get('auth_churn'):
            return None  # [daveey] an unconfirmed (auth-churn) refusal never walls the provider
        # Exact-run receipt/backoff owns the deadline; never extend it with an
        # unscoped log tail or the legacy provider-wide default. A hint the live
        # pool already disproves is mirrored as its backoff only.
        if _disproved(state, time.time(), {}):
            state = _unhinted(conn, task_id, state)
        return record_wall(state.get('provider'), max(0, state['until'] - time.time()))
    provider = ""
    try:
        for row in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'model_tier' "
            "AND (run_id IS ? OR run_id IS NULL) ORDER BY id DESC LIMIT 1", (task_id, run_id)):
            provider = _norm((json.loads(row[0] or "{}")).get("provider"))
        # Never the raw stored provider_override: a legacy row is author evidence, not the route this run used.
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
                     path: Optional[Path] = None,
                     extra_walls: Optional[dict[str, float]] = None) -> Optional[tuple[str, str, str]]:
    """``(model, provider, walled_provider)`` to run on instead, or None to leave the task alone.

    ``extra_walls`` adds walls only the caller knows (a card's own quota episode)."""
    if requires_model(task):
        return None
    walls = {**active_walls(now=now, path=path), **(extra_walls or {})}
    task_model = str(getattr(task, "model_override", "") or "").strip()
    provider = _norm(getattr(task, "provider_override", None)) or (profile_provider() if not task_model else "")
    if not provider or provider == "auto":
        return None
    if provider not in walls and not live_quota_wall(provider, task_model or profile_model()):
        return None
    # 2026-11-01: kanban.default_model moved to gpt-6-astra/openai-codex, so a Codex wall's only fallback was
    # Codex itself and every default-tier card waited out a 2.3-day wall while Anthropic had room. Try the
    # strong tier next; a fallback is usable only when its own pool can serve its model now. [daveey]
    for key in ("default", "strong"):
        model = str(cfg.get(f"{key}_model") or "").strip()
        fb = _norm(cfg.get(f"{key}_provider"))
        if not model or not fb or fb == "auto" or fb == provider or fb in walls:
            continue
        if live_quota_wall(fb, model):
            continue
        return model, fb, provider
    return None


def profile_model() -> str:
    try:
        from hermes_cli.config import load_config
        model = (load_config() or {}).get("model") or {}
        return str(model.get("default") or "").strip() if isinstance(model, dict) else ""
    except Exception:  # a broken config must not break dispatch; the worker reports it itself
        logger.debug("kanban walls: cannot read the profile model", exc_info=True)
        return ""


# A pool read that keeps failing is logged as a WARNING at most once per provider per window (every
# dispatcher tick reads it); the full traceback stays at DEBUG.
READ_FAILURE_WARN_SECONDS = 15 * 60
_READ_FAILURE_WARNED: dict[str, float] = {}


def _read_failed(check: str, provider: str, model: Optional[str], exc: BaseException) -> None:
    """Make a swallowed pool-read fault visible: each caller fails open or closed on it, silently. [daveey]"""
    logger.debug("kanban walls: %s failed for %s/%s (%s)", check, provider, model, exc, exc_info=True)
    now = time.monotonic()
    last = _READ_FAILURE_WARNED.get(provider)
    if last is not None and now - last < READ_FAILURE_WARN_SECONDS:
        return
    _READ_FAILURE_WARNED[provider] = now
    logger.warning("kanban walls: cannot read the %s OAuth pool (%s in %s for %s: %s); its live quota "
                   "evidence is ignored until it reads again", provider, type(exc).__name__, check, model, exc)


@contextlib.contextmanager
def _home_secret_scope() -> Iterator[None]:
    """Bind this home's own secret scope for a pool read when none is bound and multiplexing is on.

    A multiplexing gateway runs its dispatcher tick in an empty ``Context`` (no per-turn scope), where
    ``get_secret`` fails closed, so ``load_pool`` raised ``UnscopedSecretError`` on every tick (mini BB
    gateway, 2026-11-12). Callers judge the pool inside it too: availability checks may re-read a
    credential. A routed turn's own scope is left alone. [daveey]"""
    from agent.secret_scope import current_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope
    if not is_multiplex_active() or current_secret_scope() is not None:
        yield
        return
    from hermes_constants import get_hermes_home
    from tui_gateway.launch_profile_policy import owner_secret_scope
    home = get_hermes_home()
    token = set_secret_scope(owner_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _oauth_pool(provider: str) -> Any:
    from agent.credential_pool import load_pool
    return load_pool(provider, allowed_auth_types={"oauth"})


def live_quota_wall(provider: str, model: str) -> Optional[float]:
    """Reset time when ``provider``'s OAuth pool is quota-exhausted for ``model`` right now, else None.

    Same evidence the worker's startup resolver raises ``SubscriptionQuotaUnavailable`` on (known 429
    cooldowns on every live OAuth entry), read at claim time so no worker has to crash to learn it. [daveey]
    """
    provider = _norm(provider)
    if not provider or provider == "auto" or not model or model.startswith("class:"):
        return None
    try:
        from agent.model_routing_unavailable import quota_unavailable
        with _home_secret_scope():
            unavailable = quota_unavailable(_oauth_pool(provider), model)
    except Exception as exc:  # health: allow BLE001 -- never break dispatch over a pool read; warned
        _read_failed("live quota check", provider, model, exc)
        return None
    return float(unavailable.resets_at) if unavailable is not None else None


def live_quota_room(provider: str, model: str) -> bool:
    """True only when this gateway's OAuth pool has a live entry that can serve ``model`` right now.

    Positive evidence, unlike ``live_quota_wall() is None``: an empty, unreadable or auth-benched pool
    is no room. Pools are per gateway, so another worker's wall can be this one's room. [daveey]"""
    provider = _norm(provider)
    if not provider or provider == "auto" or not model or model.startswith("class:"):
        return False
    try:
        from agent.model_routing_unavailable import quota_room
        with _home_secret_scope():
            return bool(quota_room(_oauth_pool(provider), model))
    except Exception as exc:  # health: allow BLE001 -- never break dispatch over a pool read; warned
        _read_failed("live quota room check", provider, model, exc)
        return False


def live_pool_serving(provider: str, model: Optional[str] = None) -> bool:
    """True when ``provider``'s OAuth pool can serve ``model`` right now (``None``: any model, judged
    conservatively): no quota wall and an entry out of cooldown. Any read fault is False."""
    provider = _norm(provider)
    if not provider or provider == "auto":
        return False
    try:
        from agent.model_routing_unavailable import quota_unavailable
        with _home_secret_scope():
            pool = _oauth_pool(provider)
            return quota_unavailable(pool, model) is None and bool(pool.has_available(model=model))
    except Exception as exc:  # health: allow BLE001 -- a pool read fault keeps every hold; warned
        _read_failed("live serving check", provider, model, exc)
        return False


def prune_disproved_walls(*, now: Optional[float] = None, path: Optional[Path] = None) -> list[str]:
    """Drop provider-wide walls longer than the backoff cap that the live OAuth pool disproves.

    Only a quota hint sets a wall past ``MAX_BACKOFF_SECONDS``; a pool that serves (any model, judged
    conservatively) proves it false, e.g. a mirror of a hint born of a transient auth bench. Returns the
    providers dropped. [daveey]"""
    from hermes_cli.kanban_quota_episode import MAX_BACKOFF_SECONDS
    now = time.time() if now is None else now
    p = path or walls_path()
    walls = active_walls(now=now, path=p)
    dropped = [name for name, until in walls.items()
               if until - now > MAX_BACKOFF_SECONDS and live_pool_serving(name, None)]
    if dropped:
        current = active_walls(now=now, path=p)
        for name in dropped:
            current.pop(name, None)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(current), encoding="utf-8")
        tmp.replace(p)
        logger.info("kanban walls: dropped %s (live OAuth pool serving)", ", ".join(sorted(dropped)))
    return dropped


def drop_wall(provider: str, *, not_after: float, path: Optional[Path] = None) -> bool:
    """Remove ``provider``'s wall when it ends no later than ``not_after`` (an operator-released episode's deadline:
    the reaper copied the wall from that hint). A later wall came from other evidence and stays. True when removed.

    ``record_wall`` only ever extends, so without this a released hint's wall outlived its episode's release.
    """
    provider = _norm(provider)
    p = path or walls_path()
    walls = _load(p)
    until = walls.get(provider)
    if until is None or until > not_after + 1:
        return False
    del walls[provider]
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(walls), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("kanban walls: cannot write %s (%s)", p, exc)
        return False
    return True


def rate_limit_storm(conn: sqlite3.Connection, *, now: Optional[float] = None, prefix: Optional[str] = None) -> int:
    """``rate_limited`` runs in the last hour claimed by this host (claim ``prefix``, default this host's).

    The board is synced, so every worker's runs land here; counting them all made each host file its own copy of
    one board-wide storm under a title saying "on this host". A run with no claimer is nobody's. [daveey]"""
    now = int(time.time() if now is None else now)
    if prefix is None:
        from hermes_cli import kanban_db as _kb
        prefix = _kb._host_prefix()
    return conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE outcome = 'rate_limited' AND ended_at > ? AND substr(COALESCE("
        "NULLIF(claim_lock, ''), json_extract(CASE WHEN json_valid(metadata) THEN metadata END, '$.claimer'), ''),"
        " 1, ?) = ?",
        (now - 3600, len(prefix), prefix),
    ).fetchone()[0]


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(ts))


def alert_on_storm(conn: sqlite3.Connection, *, board: Optional[str] = None, now: Optional[float] = None) -> Optional[str]:
    """Open ONE card per host per day when more than STORM_THRESHOLD rate_limited runs landed in the last hour."""
    n = rate_limit_storm(conn, now=now)
    if n <= STORM_THRESHOLD:
        return None
    import socket
    from hermes_cli import kanban_db as _kb
    host = socket.gethostname().split(".")[0]
    day = time.strftime("%Y%m%d", time.localtime(now or time.time()))
    walls = ", ".join(f"{p} until {_utc(u)}" for p, u in active_walls(now=now).items())
    return _kb.create_task(
        conn, title=f"{host}: {n} rate_limited kanban runs in the last hour (provider quota wall)",
        body=(f"More than {STORM_THRESHOLD} workers exited on a provider quota wall in an hour on this host ({host}). "
              f"Known walls: {walls or 'none recorded'}. Check `kanban.default_model`/`default_provider`, the "
              "provider's usage limit, and cards whose body says they must stay on the walled model."),
        assignee="default", created_by="rate-limit-alert", priority=80, idempotency_key=f"rate_limit_storm:{host}:{day}",
        board=board,
    )


def tick_upkeep(conn: sqlite3.Connection, *, board: Optional[str] = None, dry_run: bool = False) -> None:
    """Per-tick wall upkeep: drop walls the live pool disproves, then alert on a rate-limit storm."""
    try:
        if not dry_run:
            prune_disproved_walls()
        alert_on_storm(conn, board=board)
    except Exception:  # health: allow BLE001 -- wall upkeep never strands the reclaim phase; traceback logged
        logger.warning("kanban: provider wall pruning / rate-limit storm alert failed", exc_info=True)
