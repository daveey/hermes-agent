"""LLM quota pacing at Kanban claim time: read the pools, hold cards the pool cannot pace. [daveey]

Called by ``kanban_db_dispatch._dispatch_lane_task`` for every ready/review candidate after the respawn guard and
before any claim (:func:`hold`). Pool math and the incident are in ``kanban_llm_pacing``.

* The card's route comes from the same read-only resolver admission uses (``resolve_dispatch_tier``); a class
  selector (``class:deep``) may run on any of its subscription routes, so it is held only when every candidate
  provider holds it. An unknown route, an ungoverned provider or a mandatory pin always starts.
* Each governed provider's OAuth pool is read (``/api/oauth/usage``, read-only) at most every
  ``read_interval`` seconds per Hermes home; samples and this host's running count on the provider are kept in
  ``<kanban home>/llm_pacing.json`` for burn rates and average concurrency.
* Dispatcher maintenance refreshes configured pools even with zero admissions; the claim path shares its interval.
* The read-only notice reader :func:`paced_pools` reads usage never, writes nothing and holds nothing.
* Any read or resolve fault starts the card (fail open, warned once per interval).
* ``kanban.llm_pacing.enabled: false`` restores the ungoverned behaviour.
* ``kanban.llm_pacing.pace_starts`` (default false): only a ``held`` pool stops a start; true also limits on ``paced``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_llm_pacing as lp

logger = logging.getLogger(__name__)

READ_DEADLINE_SECONDS = 10.0
EVENT_KIND = "llm_paced"
ADMISSION_REASON = "llm_pacing"  # the ``admission.reason`` of a stall notice for a pool the governor holds
EVENT_REPEAT_SECONDS = 3600
# A stored pace older than this many read intervals is no read: maintenance replaces it each interval,
# even without admissions, so a gap this long means this home has stopped observing the pool.
STALE_READ_INTERVALS = 3
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
    """This moment's account-wide usage windows for every readable account of ``provider``. An account whose read
    succeeded with no account window at all (``/api/oauth/usage`` reports an idle or brand-new account's
    ``five_hour`` and ``seven_day`` as ``null``, so the snapshot is not even ``available``) reads as two fresh
    windows, 0% with no reset: :func:`kanban_llm_pacing._sustainable` counts each as a full window of headroom
    (PR #3058 review). A failed read (None) or an unavailable one stays unknown."""
    readings = []
    for label, snapshot in _snapshots(provider):
        if snapshot is None or snapshot.unavailable_reason:
            continue
        account = [w for w in snapshot.windows if w.scope == "account" and w.used_percent is not None]
        if not account:
            readings += [lp.Reading(now, label, kind, 0.0, None) for kind in (lp.FIVE_HOUR, lp.SEVEN_DAY)]
        for window in account:
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


def candidate_route(conn: sqlite3.Connection, task: Any, lane: str) -> tuple[Optional[tuple], list[str]]:
    """``((model, provider, reason), providers)``: the card's next-run route and the providers it may land on
    (``[]`` = unknown, the card starts). The route is None when the tier resolver chose none (tier off, a profile
    outside its scope): the card then launches on its own pin or profile route, which the route applier never
    rewrites, so no pace move can apply to it (review 5912306106). [daveey]"""
    from hermes_cli.kanban_model_tier import profile_route, resolve_dispatch_tier
    model, provider, reason = resolve_dispatch_tier(conn, task, lane=lane)
    route = (model, provider, reason) if model else None
    if not model:
        model, provider = getattr(task, "model_override", None), getattr(task, "provider_override", None)
    if not model:
        model, provider = profile_route(getattr(task, "assignee", None))
    return route, _route_providers(model, provider)


def candidate_providers(conn: sqlite3.Connection, task: Any, lane: str) -> list[str]:
    """Providers the card's next run may land on; ``[]`` = unknown (the card starts)."""
    return candidate_route(conn, task, lane)[1]


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


def refresh_pools(conn: sqlite3.Connection) -> None:
    """Refresh configured pools before admission, even on an idle/capped home.

    Reuse the admission reader's memory/shared usage budget, including failures.
    """
    try:
        cfg = load_config()
        if not cfg.enabled:
            return
        now = time.time()
        for provider in cfg.providers:
            pace_for(conn, provider, cfg, now)
    except Exception as exc:  # health: allow BLE001 -- observation failure must never stop dispatch
        _warn("refresh", "kanban pacing: periodic refresh failed (%s); continuing dispatch", exc)


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


def _verdicts(conn, task, providers, lane, cfg, host=None) -> Optional[list[dict]]:
    """One verdict per governed candidate pool at its pace limit; None when none is (the card starts as routed).
    ``host``: the connection whose running cards count (default ``conn``): pickup judges a replica card against
    this machine's own board.

    A class route (several candidate pools) is judged pool by pool: ONE pool at its limit is enough, because the class
    allocator is weighted round-robin, not pace-aware, and would hand the card to that pool on its turn. The caller
    then moves the card to a pool of its class with room (:func:`_pace_alternate`, the limited pools as walls) or
    holds it. Since xai-oauth is governed too (b6f19b5dbc), a Grok pool with room no longer exempts a class card whose
    Claude/Codex pools are paced: it is the move's target instead. [daveey] (t_01a1deaf95f5b885)"""
    host = conn if host is None else host
    now = time.time()
    deferrable = is_deferrable(conn, task, lane)
    verdicts = []
    for provider in providers:
        pace = pace_for(host, provider, cfg, now)
        limit = lp.admit_limit(pace, avg_running(provider, now, cfg), deferrable=deferrable, cfg=cfg)
        running = local_running(host, provider)
        if limit is None or running < limit:
            continue
        verdicts.append({**pace.as_dict(), "limit": limit, "running": running, "deferrable": deferrable})
    return verdicts or None


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


def _governed(conn: sqlite3.Connection, task_id: str, lane: str, cfg: lp.PacingConfig) -> Optional[tuple[Any, list[str]]]:
    """``(task, providers)`` for a card the governor judges on ``lane``; None when it starts regardless: no such card,
    a mandatory pin, an unknown route or no provider the governor governs.

    A class route judges only its governed pools: an ungoverned pool in the same class no longer exempts the card,
    which would start it ungoverned and let the class allocator hand it to a paced pool. When any governed pool holds
    it (:func:`_verdicts`), :func:`_pace_alternate` moves it to a pool of its class with room that serves here, or it
    holds. [daveey] (t_01a1deaf95f5b885)"""
    from hermes_cli import kanban_db as kb
    task = kb.get_task(conn, task_id)
    if task is None or int(getattr(task, "model_pin_mandatory", 0) or 0):
        return None
    providers = [p for p in candidate_providers(conn, task, lane) if p in cfg.providers]
    if not providers:
        return None
    return task, providers


def _pace_alternate(conn: sqlite3.Connection, task: Any, lane: str, cfg: lp.PacingConfig,
                    host: sqlite3.Connection, verdicts: list[dict]) -> Optional[tuple[str, str, str, tuple]]:
    """``(model, provider, paced, judged)``: a route of the card's own class on a pool that is NOT ahead of pace, for a
    card a candidate pool holds (``verdicts``: the pools at their limit); ``judged`` is the ``(model, provider)`` route
    it moves from. None keeps the hold. [daveey]

    The move uses the quota-episode probe (``alternate_route``: unpinned default or strong-class routes only, the
    worker's own billing/capability eligibility, never a mandatory or ``require_model`` card, never a walled provider)
    with the holding pools as extra walls, so an ungoverned subscription pool (xai-oauth: Grok on the X Ultra
    subscription) or a governed one still behind pace takes the card. A governed target is judged by this governor
    first and only when the probe names it, so no pool is read that the card could not move to. The target must serve
    on this host's live pool and show no quota wall: never a second wall, never a provider this host holds no
    credential for. ``paced`` names the governed pools that would hold the card (``,``-joined). Pool workers
    2026-11-15: Anthropic and Codex paced to one running card each while ready cards queued for hours
    (t_01a1deaf95f5b885). Any fault keeps the hold (warned)."""
    try:
        route = candidate_route(conn, task, lane)[0]
        if route is None:
            return None  # a pin/profile fallback route is launched as is: holding is the only safe answer
        return _pace_alternate_route(conn, task, route, lane, cfg, host, verdicts)
    except Exception as exc:  # health: allow BLE001 -- a failed probe keeps today's hold, never starts a paced card
        _warn("alternate", "kanban pacing: pace alternate probe failed (%s: %s); holding", type(exc).__name__, exc)
        return None


def _pace_alternate_route(conn, task, route, lane, cfg, host, verdicts):
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_quota_episode import alternate_route
    if pw.requires_model(task):
        return None
    now = time.time()
    paced = {v["provider"]: float(v.get("until") or now + cfg.read_interval) for v in verdicts}
    unusable: dict[str, float] = {}
    model, provider, reason = route
    # A default route already moved off a wall or by its episode is still the unpinned default card.
    reason = str(reason or "")
    base = (model, provider, reason.split("+", 1)[0] if reason.startswith("default+") else reason)
    deferrable = is_deferrable(conn, task, lane)
    for _ in range(len(cfg.providers) + 4):  # every probe excludes one more provider: bounded
        moved = alternate_route(conn, task, {**unusable, **paced}, base, now)
        if not moved:
            return None
        target = pw._norm(moved[1])
        if target in paced or target in unusable:
            return None  # the probe ignored its walls: never trust it
        if not pw.live_pool_serving(target, moved[0]) or pw.live_quota_wall(target, moved[0]):
            unusable[target] = now
            continue
        if target in cfg.providers:
            pace = pace_for(host, target, cfg, now)
            limit = lp.admit_limit(pace, avg_running(target, now, cfg), deferrable=deferrable, cfg=cfg)
            if limit is not None and local_running(host, target) >= limit:
                paced[target] = float(pace.until or now + cfg.read_interval)
                continue
        return moved[0], target, ",".join(sorted(paced)), (model, provider)
    return None


def _stored_pace(entry: Any, now: float, cfg: lp.PacingConfig) -> Optional[lp.PoolPace]:
    """The pace a dispatcher on this home stored for one provider; None when it is absent, malformed, stamped in the
    future or older than :data:`STALE_READ_INTERVALS` read intervals."""
    read_at = entry.get("read_at") if isinstance(entry, dict) else None
    if not isinstance(read_at, (int, float)):
        return None
    return lp.PoolPace.from_dict(entry.get("pace")) if 0 <= now - read_at < STALE_READ_INTERVALS * cfg.read_interval else None


def paced_pools(conn: sqlite3.Connection, task_id: str) -> Optional[list[lp.PoolPace]]:
    """The pools holding ready card ``task_id``, for a stall notice: one :class:`~kanban_llm_pacing.PoolPace` per
    candidate provider when EVERY candidate is stored as ``paced`` or ``held``; else None (no positive read).

    A notice describes the hold, it never makes one: this reads the pace the dispatchers of this home stored
    (``llm_pacing.json``; pool workers draw the same accounts from the hub's relay), makes no usage read and writes
    nothing. It judges the card as :func:`hold` does (:func:`_governed`) and, like :func:`_verdicts`, finds no hold as
    soon as one candidate is open, unknown or unread: the card may start there. The pool's state, not this host's slot
    math: the hosts that would run the card keep their own concurrency, so a held pool admits nothing anywhere and a
    paced pool limits every host. Faults propagate; the caller fails open."""
    cfg = load_config()
    judged = _governed(conn, task_id, "ready", cfg) if cfg.enabled else None
    if judged is None:
        return None
    stored, now = _load_state(state_path()), time.time()
    paces: list[lp.PoolPace] = []
    for provider in judged[1]:
        pace = _stored_pace(stored.get(provider), now, cfg)
        if pace is None or pace.state not in (lp.PACED, lp.HELD):
            return None
        paces.append(pace)
    return paces


def held_pools(section: Any, now: float) -> dict[str, float]:
    """``provider -> until`` (``inf`` when no reset is known) for every governed pool this home's dispatchers stored as
    ``held`` and fresh (:func:`_stored_pace`), under the ``kanban.llm_pacing`` ``section``; pacing off = none.

    The route resolver's read-only view (``kanban_paced_reroute``: a far hold moves a route to an unheld peer). Like
    :func:`paced_pools` it reads the stored pace only: no usage read, no write, no hold."""
    cfg = lp.config_from(section)
    if not cfg.enabled:
        return {}
    now_held: dict[str, float] = {}
    for provider, entry in _load_state(state_path()).items():
        name = str(provider).strip().lower()
        pace = _stored_pace(entry, now, cfg) if name in cfg.providers else None
        if pace is None or pace.state != lp.HELD:
            continue
        until = pace.until
        now_held[name] = float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else math.inf
    return now_held


def admission(paces: list[lp.PoolPace]) -> dict[str, Any]:
    """The ``admission`` a stall notice carries for :func:`paced_pools`: each pool's state, and ``until`` when known."""
    return {"reason": ADMISSION_REASON, "status": "held", "providers": [
        {"provider": p.provider, "state": p.state, **({"until": int(p.until)} if p.until else {})} for p in paces]}


def admission_skip(stored: Any) -> Optional[dict[str, Any]]:
    """The ``llm_paced`` skip a stored :func:`admission` stands for, so a notice words and advises it as the
    dispatcher's own hold does (``kanban_dispatch_skip``): every pool, and the earliest ``until``. None for any other
    admission and for a malformed one."""
    if not isinstance(stored, dict) or stored.get("reason") != ADMISSION_REASON:
        return None
    listed = stored.get("providers")
    pools = [p for p in listed if isinstance(p, dict) and p.get("provider") and p.get("state") in (lp.PACED, lp.HELD)
             ] if isinstance(listed, list) else []
    if not pools:
        return None
    until = [p["until"] for p in pools if isinstance(p.get("until"), (int, float)) and not isinstance(p["until"], bool)]
    return {"reason": EVENT_KIND, "detail": ", ".join(f"{p['provider']} {p['state']}" for p in pools),
            **({"until": min(until)} if until else {})}


def hold(conn: sqlite3.Connection, row: Any, lane: str, result: Any, *, dry_run: bool = False) -> bool:
    """True when the card must wait for its provider's pool to come back to pace (recorded on ``result``)."""
    try:
        if not dry_run:
            # A fresh judgment: an offer from an earlier tick whose claim never happened must not steer this one.
            from hermes_cli.kanban_pace_reroute import withdraw
            withdraw(row["id"])
        cfg = load_config()
        judged = _governed(conn, row["id"], lane, cfg) if cfg.enabled else None
        if judged is None:
            return False
        task, providers = judged
        verdicts = _verdicts(conn, task, providers, lane, cfg)
        if verdicts is None:
            return False
        moved = _pace_alternate(conn, task, lane, cfg, conn, verdicts)
        if moved is not None:
            # Start it on a pool with room instead of holding it; the applier moves the route after the claim.
            from hermes_cli.kanban_pace_reroute import offer
            if not dry_run:
                offer(task.id, moved[3], moved[:3])
            logger.info("kanban pacing: %s pools %s ahead of pace; starting on %s/%s", task.id, moved[2], moved[1],
                        moved[0])
            return False
        _record(conn, task.id, verdicts, result, dry_run=dry_run, cfg=cfg)
        return True
    except Exception as exc:  # health: allow BLE001 -- the governor fails open; a fault never holds a card
        _warn("hold", "kanban pacing: governor check failed (%s: %s); dispatching ungoverned", type(exc).__name__, exc)
        logger.debug("kanban pacing: governor check failed", exc_info=True)
        return False


def pickup_hold(local: sqlite3.Connection, replica: sqlite3.Connection, card: Any) -> Optional[str]:
    """Pickup's read-only twin of :func:`hold`: ``"<provider> <state>, ..."`` when this machine's dispatcher would
    pace-hold the foreign ``card`` right now, else None. The card is judged on the replica's copy, the running
    count on ``local`` (this machine's board), so a copy that only pacing would hold is never taken into the pickup
    window (worker-6 2026-11-15: 9 paced codex cards hid a runnable anthropic card at rank 16). Records nothing;
    any fault returns None (fail open: the local dispatcher still decides after the copy)."""
    try:
        cfg = load_config()
        judged = _governed(replica, card["id"], card["status"], cfg) if cfg.enabled else None
        if judged is None:
            return None
        verdicts = _verdicts(replica, judged[0], judged[1], card["status"], cfg, host=local)
        if verdicts is not None and _pace_alternate(replica, judged[0], card["status"], cfg, local, verdicts):
            return None  # the local claim starts it on a pool with room (hold)
        return None if verdicts is None else ", ".join(f"{v['provider']} {v['state']}" for v in verdicts)
    except Exception as exc:  # health: allow BLE001 -- a hold prediction must never stop a pickup
        _warn("pickup", "kanban pacing: pickup hold check failed (%s: %s); copying ungoverned", type(exc).__name__, exc)
        return None
