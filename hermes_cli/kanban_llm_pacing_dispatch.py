"""LLM quota pacing at Kanban claim time: read the pools, hold cards the pool cannot pace. [daveey]

Called by ``kanban_db_dispatch._dispatch_lane_task`` for every ready/review candidate after the respawn guard and
before any claim (:func:`hold`). Pool math and the incident are in ``kanban_llm_pacing``.

* The card's route comes from the same read-only resolver admission uses (``resolve_dispatch_tier``); a class
  selector (``class:deep``) may run on any of its subscription routes, so it is held only when every candidate
  provider holds it. An unknown route, an ungoverned provider or a mandatory pin always starts.
* Each governed provider's OAuth pool is read (``/api/oauth/usage``, read-only) at most every
  ``read_interval`` seconds per Hermes home; samples and this host's running count on the provider are kept in
  ``<kanban home>/llm_pacing.json`` for burn rates and average concurrency.
* Only Kanban dispatch consults this module: chats, cron and notice delivery never do.
* Any read or resolve fault starts the card (fail open, warned once per interval).
* ``kanban.llm_pacing.enabled: false`` restores the ungoverned behaviour.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_llm_pacing as lp

logger = logging.getLogger(__name__)

READ_DEADLINE_SECONDS = 10.0
EVENT_KIND = "llm_paced"
EVENT_REPEAT_SECONDS = 3600
_STATE_FILE = "llm_pacing.json"
_CACHE: dict[str, tuple[float, lp.PoolPace]] = {}
_CLASS_CACHE: dict[str, tuple[float, list[str]]] = {}
_WARNED: dict[str, float] = {}


def load_config() -> lp.PacingConfig:
    from hermes_cli.kanban_model_tier import load_tier_config
    return lp.config_from(load_tier_config().get("llm_pacing"))


def state_path() -> Path:
    from hermes_cli import kanban_db as kb
    return kb.kanban_home() / _STATE_FILE


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("kanban pacing: cannot write %s", path, exc_info=True)


def _warn(key: str, message: str, *args: Any) -> None:
    now = time.monotonic()
    if now - _WARNED.get(key, -1e9) >= 900:
        _WARNED[key] = now
        logger.warning(message, *args)


def _snapshots(provider: str) -> list[tuple[str, Any]]:
    """``(account label, usage snapshot or None)`` for every enabled, live OAuth account (bounded, read-only)."""
    from concurrent.futures import ThreadPoolExecutor, wait

    from agent.account_usage import fetch_account_usage
    from agent.credential_pool import STATUS_DEAD
    from agent.credential_pool_disabled import enabled_entries
    from hermes_cli.kanban_provider_walls import _home_secret_scope, _oauth_pool
    with _home_secret_scope():
        entries = [(entry.label or entry.id[:8], entry.runtime_api_key)
                   for entry in enabled_entries(provider, _oauth_pool(provider).entries())
                   if entry.auth_type == "oauth" and entry.last_status != STATUS_DEAD and entry.runtime_api_key]
    if not entries:
        return []
    pool = ThreadPoolExecutor(max_workers=len(entries))
    try:
        futures = [pool.submit(fetch_account_usage, provider, api_key=key, read_only=True) for _, key in entries]
        wait(futures, timeout=READ_DEADLINE_SECONDS)
        return [(label, f.result() if f.done() and not f.exception() else None)
                for (label, _), f in zip(entries, futures)]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def read_pool(provider: str, now: float) -> list[lp.Reading]:
    """This moment's account-wide usage windows for every readable account of ``provider``."""
    readings = []
    for label, snapshot in _snapshots(provider):
        if snapshot is None or not snapshot.available:
            continue
        for window in snapshot.windows:
            if window.scope != "account" or window.used_percent is None:
                continue
            reset = window.reset_at.timestamp() if window.reset_at is not None else None
            readings.append(lp.Reading(now, label, lp.window_kind(window.label), float(window.used_percent), reset))
    return readings


def class_providers(model: str) -> list[str]:
    """Providers a ``class:<level>`` selector may allocate (the allocator's own billing filter), cached 60 s."""
    cached = _CLASS_CACHE.get(model)
    if cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    from agent.model_routing import eligible_routes
    from hermes_cli.config_effective import load_user_config_effective
    policy = (load_user_config_effective() or {}).get("model_routing") or {}
    level = model.removeprefix("class:")
    providers = sorted({str(r["provider"]).lower() for r in eligible_routes(policy, level)}) if policy.get("enabled") else []
    _CLASS_CACHE[model] = (time.monotonic(), providers)
    return providers


def _route_providers(model: Optional[str], provider: Optional[str]) -> list[str]:
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_tier import infer_provider
    if not model:
        return []
    if is_class_selector(model) or str(provider or "").lower() == "auto":
        return class_providers(model)
    name = str(provider or infer_provider(model) or "").strip().lower()
    return [name] if name else []


def candidate_providers(conn: sqlite3.Connection, task: Any, lane: str) -> list[str]:
    """Providers the card's next run may land on; ``[]`` = unknown (the card starts)."""
    from hermes_cli.kanban_model_tier import profile_route, resolve_dispatch_tier
    model, provider, _ = resolve_dispatch_tier(conn, task, lane=lane)
    if not model:
        model, provider = getattr(task, "model_override", None), getattr(task, "provider_override", None)
    if not model:
        model, provider = profile_route(getattr(task, "assignee", None))
    return _route_providers(model, provider)


def local_running(conn: sqlite3.Connection, provider: str) -> int:
    """This host's running cards whose run routed to ``provider`` (a class run counts for each candidate)."""
    from hermes_cli import kanban_db as kb
    prefix = kb._host_prefix()
    rows = conn.execute(
        "SELECT e.payload FROM tasks t JOIN task_events e ON e.task_id = t.id AND e.run_id = t.current_run_id "
        "AND e.kind = 'run_route' WHERE t.status = 'running' AND substr(t.claim_lock, 1, ?) = ?",
        (len(prefix), prefix)).fetchall()
    count = 0
    for (payload,) in rows:
        route = kb._json_dict(payload)
        if provider in _route_providers(route.get("model"), route.get("provider")):
            count += 1
    return count


def _history(entry: dict, now: float, cfg: lp.PacingConfig) -> list[lp.Reading]:
    rows = (lp.Reading.from_row(row) for row in entry.get("samples") or [])
    return [r for r in rows if r is not None and now - 2 * cfg.rate_window <= r.ts < now]


def _refresh(conn: sqlite3.Connection, provider: str, cfg: lp.PacingConfig, now: float) -> lp.PoolPace:
    path = state_path()
    state = _load_state(path)
    found = state.get(provider)
    entry: dict = found if isinstance(found, dict) else {}
    shared = lp.PoolPace.from_dict(entry.get("pace"))
    if shared is not None and now - float(entry.get("read_at") or 0) < cfg.read_interval:
        return shared  # another dispatcher on this home read it already
    try:
        current = read_pool(provider, now)
    except Exception as exc:  # health: allow BLE001 -- a usage read fault never stops dispatch (fail open)
        _warn(f"read:{provider}", "kanban pacing: cannot read the %s usage windows (%s); dispatch is ungoverned "
              "until it reads again", provider, exc)
        current = []
    history = _history(entry, now, cfg)
    pace = lp.pool_pace(provider, current, history, cfg)
    running = [row for row in entry.get("running") or [] if now - cfg.rate_window <= float(row[0]) < now]
    state[provider] = {"read_at": now, "pace": pace.as_dict(), "running": running + [[now, local_running(conn, provider)]],
                       "samples": [r.as_row() for r in history + current]}
    _save_state(path, state)
    if pace.state != (shared.state if shared else None):
        logger.info("kanban pacing: %s pool %s (factor %s, until %s): %s", provider, pace.state, pace.factor,
                    pace.until, pace.detail)
    return pace


def pace_for(conn: sqlite3.Connection, provider: str, cfg: lp.PacingConfig, now: float) -> lp.PoolPace:
    cached = _CACHE.get(provider)
    if cached is not None and now - cached[0] < cfg.read_interval:
        return cached[1]
    pace = _refresh(conn, provider, cfg, now)
    _CACHE[provider] = (now, pace)
    return pace


def avg_running(provider: str, now: float, cfg: lp.PacingConfig) -> float:
    """This host's average concurrency on ``provider`` over ``rate_window`` (the samples the burn was measured on)."""
    entry = _load_state(state_path()).get(provider) or {}
    counts = [float(n) for ts, n in entry.get("running") or [] if now - cfg.rate_window <= float(ts) <= now]
    return sum(counts) / len(counts) if counts else 0.0


def is_deferrable(conn: sqlite3.Connection, task: Any, lane: str) -> bool:
    """Review-lane work, negative priority and scheduled wakes yield first when the pool is paced."""
    if lane == "review" or int(getattr(task, "priority", 0) or 0) < 0:
        return True
    from hermes_cli.kanban_continuation import CONTINUATION_WOKEN_EVENT
    row = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND kind IN (?, 'claimed', 'spawned') "
                       "ORDER BY id DESC LIMIT 1", (task.id, CONTINUATION_WOKEN_EVENT)).fetchone()
    return row is not None and row[0] == CONTINUATION_WOKEN_EVENT


def _verdicts(conn, task, providers, lane, cfg) -> Optional[list[dict]]:
    """One verdict per governed candidate provider, or None as soon as one provider admits the card."""
    now = time.time()
    deferrable = is_deferrable(conn, task, lane)
    verdicts = []
    for provider in providers:
        pace = pace_for(conn, provider, cfg, now)
        limit = lp.admit_limit(pace, avg_running(provider, now, cfg), deferrable=deferrable, cfg=cfg)
        running = local_running(conn, provider)
        if limit is None or running < limit:
            return None
        verdicts.append({**pace.as_dict(), "limit": limit, "running": running, "deferrable": deferrable})
    return verdicts


def _record(conn: sqlite3.Connection, task_id: str, verdicts: list[dict], result: Any, *, dry_run: bool,
            cfg: lp.PacingConfig) -> None:
    from hermes_cli import kanban_db as kb
    now = time.time()
    until = min((v["until"] for v in verdicts if v.get("until")), default=None)
    result.respawn_guarded.append((task_id, lp.GUARD_REASON))
    result.clock_held[task_id] = (lp.GUARD_REASON, int(until or now + cfg.read_interval))
    # The tick's skip record (``kanban_dispatch_skip``): notices and diagnostics name the hold, not "unrecorded".
    detail = ", ".join(f"{v['provider']} {v['state']}" for v in verdicts)
    result.llm_paced.append((task_id, detail, int(until) if until else None, {"providers": [
        {k: v.get(k) for k in ("provider", "state", "limit", "running")} for v in verdicts]}))
    if dry_run:
        return
    last = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = ? "
                        "ORDER BY id DESC LIMIT 1", (task_id, EVENT_KIND)).fetchone()
    key = [(v["provider"], v["state"], v["limit"]) for v in verdicts]
    if last is not None and now - float(last[1] or 0) < EVENT_REPEAT_SECONDS:
        prior = kb._json_dict(last[0]).get("providers") or []
        if [(v.get("provider"), v.get("state"), v.get("limit")) for v in prior] == key:
            return
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, EVENT_KIND, {"providers": verdicts, "until": until})


def hold(conn: sqlite3.Connection, row: Any, lane: str, result: Any, *, dry_run: bool = False) -> bool:
    """True when the card must wait for its provider's pool to come back to pace (recorded on ``result``)."""
    try:
        cfg = load_config()
        if not cfg.enabled:
            return False
        from hermes_cli import kanban_db as kb
        task = kb.get_task(conn, row["id"])
        if task is None or int(getattr(task, "model_pin_mandatory", 0) or 0):
            return False
        providers = candidate_providers(conn, task, lane)
        if not providers or any(p not in cfg.providers for p in providers):
            return False
        verdicts = _verdicts(conn, task, providers, lane, cfg)
        if verdicts is None:
            return False
        _record(conn, task.id, verdicts, result, dry_run=dry_run, cfg=cfg)
        return True
    except Exception as exc:  # health: allow BLE001 -- the governor fails open; a fault never holds a card
        _warn("hold", "kanban pacing: governor check failed (%s: %s); dispatching ungoverned", type(exc).__name__, exc)
        logger.debug("kanban pacing: governor check failed", exc_info=True)
        return False
