"""Ratio routing: default-tier cards start on the pool furthest below its planned budget share. [daveey]

Each host's dispatcher plans every governed OAuth pool's sustainable token rate (``llm_pacing.json`` ``v2``,
``kanban_llm_pacing``); ``v2.pools[p].ratio`` is that pool's share of the global rate. Before this, every unpinned
default card ran on ``kanban.default_model`` (gpt-6-astra on openai-codex) until its pool reached reserve, and only
then moved (``kanban_paced_reroute``): one pool drained to its floor while the others sat idle.

Rule (:func:`pick`), applied by ``kanban_model_tier.resolve_dispatch_tier`` to an unpinned ``default`` route only (no
``model_override``/``provider_override``, not a review run, no ``require_model: true`` line):

1. **Routes.** The ``model_routing.routes`` class level of ``kanban.default_model`` (the assignee's policy, as the
   quota-episode probe reads it) names the pools, in registry order: the level a ``class:<level>`` default names, else
   the one level that lists the concrete default model. Each pool runs ``kanban.llm_pacing.ratio_route_models[pool]``
   when that is one of the pool's eligible routes in the level, else its first eligible route. A concrete default
   model in no level (or in two) turns ratio routing off. A pick is always a concrete route; failing open keeps the
   base (under a class default, the ``class:<level>``/``auto`` selector itself).
2. **Eligible pools.** Governed (``kanban.llm_pacing.providers``), with a ``v2`` entry whose ``ratio`` is above 0, not
   ``at_reserve`` and with a known ``rate_tokens_per_s``; the provider enabled; serving on this host's live pool
   (``live_pool_serving``: a credential here and no live quota wall for the model, one pool load); no provider wall
   (``active_walls``). Each pool's own ``v2`` reading must be fresh too (:func:`fresh_v2`).
3. **Choice by deficit.** Targets are the eligible pools' ratios renormalized to 1. The share is each eligible pool's
   part of this host+board's default-tier starts over the last :data:`SHARE_WINDOW_SECONDS` (``run_route`` receipts
   whose ``tier_reason`` starts with ``default`` and whose run this host claimed; a receipt with no provider counts
   toward ``kanban.default_provider``). The pool with the largest ``target - share`` wins; ties go to registry order.
4. **Fail open** (today's route) when ``kanban.llm_pacing.ratio_routing`` is false, pacing is off, ``v2`` is missing,
   malformed or older than :data:`STALE_SECONDS`, no pool is eligible, or anything raises (logged).

Start counts are read once per dispatch tick (:func:`begin_tick` drops the memo, the first pick reads the board) and
memoized per board, so the preview, the governor and admission in one tick choose alike; :func:`note_start` adds each
default start recorded by this process, so the next card in the same tick sees it. The count is bounded: a binary
search on the event id finds the window's first event (``task_events`` has no ``(kind, created_at)`` index), and only
that tail is scanned. Read-only: no write, no allocation.

The chosen route is ``(model, provider, "default")`` to every later rule (episode, walls, pace), and the final
``tier_reason`` is tagged ``default+ratio:<pool>[+<later move>]``; :func:`receipt` adds the pool, target, share and
deficit to the ``model_tier`` and ``run_route`` receipts.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

REASON = "ratio"
NOTE_PREFIX = "next run on "  # ``kanban_paced_reroute.route_words``: "next run on <pool> (budget share): ..."
STALE_SECONDS = 20 * 60
SHARE_WINDOW_SECONDS = 6 * 3600
MEMO_TTL_SECONDS = 60.0  # a memo no tick renewed (a CLI card read, a stalled dispatcher) is re-read after this
MAX_COUNTED_STARTS = 20000  # a hard bound on the window scan; far above any board's six-hour start count
_WARN_EVERY_SECONDS = 900.0


@dataclass
class _Memo:
    read_at: float  # time.monotonic() of the count
    counts: dict[str, int]
    default_provider: str  # a receipt with no provider counts toward it
    picks: dict[str, dict[str, Any]] = field(default_factory=dict)  # task id -> its last pick's receipt


_LOCK = threading.Lock()
_MEMOS: dict[str, _Memo] = {}
_OFF = threading.Event()  # set when a tick could not start its memo: picks fail open until a tick starts cleanly
_WARNED: dict[str, float] = {}


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) \
        else None


def _section(cfg: Optional[dict]) -> dict:
    section = (cfg or {}).get("llm_pacing")
    return section if isinstance(section, dict) else {}


def enabled(cfg: Optional[dict]) -> bool:
    """``kanban.llm_pacing.ratio_routing`` (default true) and pacing itself on: only an explicit ``false`` disables."""
    from hermes_cli import kanban_llm_pacing as lp
    section = _section(cfg)
    return section.get("ratio_routing", True) is not False and lp.config_from(section).enabled


def _key(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2] or f"memory:{id(conn)}")
    return f"memory:{id(conn)}"


def begin_tick(conn: sqlite3.Connection) -> None:
    """A dispatch tick starts on this board: the next pick re-reads the start counts."""
    key = _key(conn)
    with _LOCK:
        _MEMOS.pop(key, None)
    _OFF.clear()


def off_this_tick() -> None:
    """:func:`begin_tick` failed: no pick may trust a memo it could not reset, so ratio routing fails open (every
    board in this process) until the next tick starts cleanly."""
    with _LOCK:
        _MEMOS.clear()
    _OFF.set()


def _first_event_id_since(conn: sqlite3.Connection, since: float) -> Optional[int]:
    """The smallest event id whose row (or the first row after it) was created at or after ``since``: a binary search
    on the primary key, about log2(events) seeks. It assumes ids grow with ``created_at``. Where they do not (a
    replica's synced row may carry an older stamp), the bound may land late and skip in-window rows below it; those
    rows are not refiltered or counted. The scan's own ``created_at`` filter only drops old rows above the bound."""
    # Two statements: SQLite answers a lone MIN or MAX of the rowid from the b-tree edge, both together by a full scan.
    hi = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
    if hi is None:
        return None
    lo = conn.execute("SELECT MIN(id) FROM task_events").fetchone()[0]
    hi += 1
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute("SELECT created_at FROM task_events WHERE id >= ? ORDER BY id LIMIT 1", (mid,)).fetchone()
        stamp = _number(row[0]) if row is not None else None
        if row is None or (stamp is not None and stamp >= since):
            hi = mid
        else:
            lo = mid + 1
    return lo


def count_starts(conn: sqlite3.Connection, now: float, default_provider: str) -> dict[str, int]:
    """``provider -> default-tier starts`` this host claimed on this board in the last :data:`SHARE_WINDOW_SECONDS`."""
    from hermes_cli.kanban_host_identity import is_local_claim
    from hermes_cli import kanban_db as kb
    since = now - SHARE_WINDOW_SECONDS
    first = _first_event_id_since(conn, since)
    counts: dict[str, int] = {}
    if first is None:
        return counts
    prefix = kb._host_prefix()
    # ``+e.kind`` keeps the planner on the id range (the window's tail), never the whole ``run_route`` history.
    rows = conn.execute(
        "SELECT e.payload, r.claim_lock FROM task_events e LEFT JOIN task_runs r ON r.id = e.run_id "
        "WHERE e.id >= ? AND +e.kind = 'run_route' AND e.created_at >= ? ORDER BY e.id DESC LIMIT ?",
        (first, int(since), MAX_COUNTED_STARTS))
    for payload, lock in rows:
        if not is_local_claim(lock, prefix):
            continue
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or not str(data.get("tier_reason") or "").startswith("default"):
            continue
        provider = str(data.get("provider") or "").strip().lower() or default_provider
        if provider:
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def _memo(conn: sqlite3.Connection, default_provider: str, now: float) -> _Memo:
    key = _key(conn)
    with _LOCK:
        found = _MEMOS.get(key)
        if (found is not None and found.default_provider == default_provider
                and time.monotonic() - found.read_at < MEMO_TTL_SECONDS):
            return found
    memo = _Memo(time.monotonic(), count_starts(conn, now, default_provider), default_provider)
    with _LOCK:
        _MEMOS[key] = memo
    return memo


def note_start(conn: sqlite3.Connection, provider: Optional[str], reason: str) -> None:
    """A default-tier start was just recorded (its ``run_route`` receipt is written): count it in this board's memo,
    so the next card in the same tick sees it. No memo = nothing to keep in step (the next read counts the receipt)."""
    if not str(reason or "").startswith("default"):
        return
    key = _key(conn)
    with _LOCK:
        memo = _MEMOS.get(key)
        name = str(provider or "").strip().lower() or (memo.default_provider if memo is not None else "")
        if memo is not None and name:
            memo.counts[name] = memo.counts.get(name, 0) + 1


def receipt(conn: sqlite3.Connection, task_id: str, reason: str) -> Optional[dict[str, Any]]:
    """``{pool, model, target, share, deficit}`` of ``task_id``'s ratio pick when ``reason`` carries one, else None."""
    marker = f"+{REASON}:"
    if marker not in str(reason or ""):
        return None
    pool = str(reason).split(marker, 1)[1].split("+", 1)[0]
    with _LOCK:
        memo = _MEMOS.get(_key(conn))
        found = memo.picks.get(task_id) if memo is not None else None
    return dict(found) if found is not None and found.get("pool") == pool else None


def tag(route: tuple, pool: str) -> tuple:
    """``route`` with its ``default...`` reason tagged ``default+ratio:<pool>...``; any other reason is unchanged."""
    model, provider, reason = route
    if not str(reason).startswith("default"):
        return route
    return model, provider, f"default+{REASON}:{pool}{str(reason)[len('default'):]}"


def _warn(key: str, message: str, *args: Any) -> None:
    now = time.monotonic()
    if now - _WARNED.get(key, -math.inf) >= _WARN_EVERY_SECONDS:
        _WARNED[key] = now
        logger.warning(message, *args, exc_info=True)
    else:
        logger.debug(message, *args, exc_info=True)


def _fresh(stamp: Any, now: float) -> bool:
    value = _number(stamp)
    return value is not None and 0 <= now - value <= STALE_SECONDS


def fresh_v2(now: float) -> Optional[dict]:
    """This home's fresh ``v2`` document (with this process's newer pools laid over it), else None. Only its pools
    read within :data:`STALE_SECONDS` are kept: ``assemble_v2`` carries every other pool's last entry forward, so a
    fresh ``computed_at`` says nothing about a pool this home stopped reading. A pool's age is its own ``read_at``
    (as ``reserve_hold`` judges it), else its legacy provider block's ``read_at``."""
    from hermes_cli.kanban_llm_pacing_dispatch import _hold_state
    state = _hold_state()
    v2 = state.get("v2")
    if not isinstance(v2, dict) or not isinstance(v2.get("pools"), dict) or not _fresh(v2.get("computed_at"), now):
        return None

    def read_at(provider: str, pool: dict) -> Any:
        if "read_at" in pool:
            return pool["read_at"]
        legacy = state.get(provider)
        return legacy.get("read_at") if isinstance(legacy, dict) else None

    pools = {p: v for p, v in v2["pools"].items() if isinstance(v, dict) and _fresh(read_at(p, v), now)}
    return {**v2, "pools": pools}


def _pool_routes(task: Any, default_model: str) -> list[tuple[str, list[str]]]:
    """``[(pool, [eligible models])]`` in registry order, from the class level of ``default_model``: the level a
    ``class:<level>`` selector names, else the one level that lists the concrete model."""
    from agent.model_routing import AUTOMATIC_CLASSES, ModelRoutingError, eligible_routes, is_class_selector, validate_policy
    from agent.model_routing_agent import class_requirements
    from hermes_cli.config import is_provider_enabled
    from hermes_cli.kanban_model_assignment import _assignee_config
    if not default_model:
        return []
    config = _assignee_config(getattr(task, "assignee", None)) or {}
    policy = config.get("model_routing") or {}
    try:
        validate_policy(policy)
    except ModelRoutingError:
        return []
    if not policy.get("enabled"):
        return []
    if is_class_selector(default_model):
        levels = [level for level in [default_model.removeprefix("class:")] if level in AUTOMATIC_CLASSES]
    else:
        levels = [level for level, routes in (policy.get("routes") or {}).items()
                  if level in AUTOMATIC_CLASSES and any(r.get("model") == default_model for r in routes)]
    if len(levels) != 1:
        return []
    providers = config.get("providers") or {}
    providers = providers if isinstance(providers, dict) else {}

    def provider_enabled(route: dict) -> bool:
        block = providers.get(route["provider"], {})
        return is_provider_enabled(block if isinstance(block, dict) else {})

    pools: dict[str, list[str]] = {}
    for route in eligible_routes(policy, levels[0], class_requirements(config), eligible=provider_enabled):
        pools.setdefault(str(route["provider"]).strip().lower(), []).append(route["model"])
    return list(pools.items())


def _eligible(pool: str, model: str, entry: Any, walled: dict[str, float]) -> Optional[float]:
    """The pool's ratio when it may take a default card now (module rule 2), else None."""
    from hermes_cli import kanban_provider_walls as pw
    if not isinstance(entry, dict) or entry.get("at_reserve") is True:
        return None
    ratio = _number(entry.get("ratio"))
    if ratio is None or ratio <= 0 or _number(entry.get("rate_tokens_per_s")) is None:
        return None
    # ``live_pool_serving`` is the one credential-pool load per pool and pick: it is False on a quota wall for
    # ``model`` (``quota_unavailable``, the evidence ``live_quota_wall`` reads) as well as with no live credential.
    if pw.wall_until(walled, pool, model) or not pw.live_pool_serving(pool, model):
        return None
    return ratio


def choose(order: list[str], targets: dict[str, float], counts: dict[str, int]) -> tuple[str, float, float, float]:
    """``(pool, target, share, deficit)``: the pool of ``order`` furthest below its renormalized target share of the
    starts in ``counts``; ties go to the first in ``order``. Pure."""
    weight = sum(targets[p] for p in order)
    starts = sum(counts.get(p, 0) for p in order)
    rows = []
    for pool in order:
        target = targets[pool] / weight
        share = counts.get(pool, 0) / starts if starts else 0.0
        rows.append((pool, target, share, target - share))
    return max(rows, key=lambda row: row[3])  # max keeps the first of equal deficits: registry order


def _pick(conn, task, base, cfg, now):
    from hermes_cli import kanban_llm_pacing as lp
    from hermes_cli import kanban_provider_walls as pw
    v2 = fresh_v2(now)
    if v2 is None:
        return None
    routes = _pool_routes(task, str(base[0] or ""))
    governed = lp.config_from(_section(cfg)).providers
    overrides = _section(cfg).get("ratio_route_models")
    overrides = overrides if isinstance(overrides, dict) else {}
    walled = pw.active_walls(now=now)
    models: dict[str, str] = {}
    targets: dict[str, float] = {}
    for pool, eligible_models in routes:
        if pool not in governed:
            continue
        wanted = str(overrides.get(pool) or "").strip()
        model = wanted if wanted in eligible_models else eligible_models[0]
        ratio = _eligible(pool, model, v2["pools"].get(pool), walled)
        if ratio is not None:
            models[pool], targets[pool] = model, ratio
    if not targets:
        return None
    order = [pool for pool, _ in routes if pool in targets]
    from agent.model_routing import is_class_selector
    from hermes_cli.kanban_model_tier import infer_provider
    # A receipt with no provider is a default start on ``default_provider``; under a class default the worker chose
    # the provider and nobody recorded it, so such a receipt counts toward no pool.
    default_provider = "" if is_class_selector(base[0]) else (pw._norm(base[1]) or pw._norm(infer_provider(str(base[0]))))
    memo = _memo(conn, default_provider, now)
    pool, target, share, deficit = choose(order, targets, memo.counts)
    detail = {"pool": pool, "model": models[pool], "target": round(target, 4), "share": round(share, 4),
              "deficit": round(deficit, 4), "starts": sum(memo.counts.get(p, 0) for p in order)}
    with _LOCK:
        memo.picks[str(task.id)] = detail
    return models[pool], pool


def pick(conn: sqlite3.Connection, task: Any, base: tuple, *, lane: str,
         cfg: Optional[dict]) -> Optional[tuple[str, str]]:
    """``(model, pool)`` for an unpinned default ``base`` route (module rule), else None (keep ``base``). Never raises:
    a fault keeps today's route (logged)."""
    try:
        if base[2] != "default" or _OFF.is_set() or not enabled(cfg):
            return None
        if str(getattr(task, "model_override", None) or "").strip() or \
                str(getattr(task, "provider_override", None) or "").strip():
            return None
        from hermes_cli.kanban_provider_walls import requires_model
        from hermes_cli.kanban_review_model import is_review_run
        if requires_model(task) or is_review_run(conn, task, lane):
            return None
        return _pick(conn, task, base, cfg, time.time())
    except Exception:  # health: allow BLE001 -- ratio routing fails open: the card keeps today's default route
        _warn("pick", "kanban ratio routing: pick failed for %s; keeping the default route", getattr(task, "id", "?"))
        return None
