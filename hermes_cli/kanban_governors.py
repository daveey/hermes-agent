"""Dispatch governors: one registry, and a per-board snapshot of what each tick decided. [daveey]

A ready card can wait behind about forty different rules (host slots, lane gates, LLM pacing, labels, walls,
budgets, drains). Each rule records its own evidence in its own shape, so "why is this card idle" meant reading
several event kinds and guessing. This module names every rule once and records, per dispatcher tick, which rule
stopped each idle card.

* :func:`registry` lists every governor in dispatcher order: ``id``, ``label``, ``scope`` (host / board / lane / pool /
  provider / profile / machine / card), ``source`` (``file:function`` of the predicate the dispatcher calls),
  ``threshold`` (the config key or constant that sets its limit) and ``kind`` (``hold`` stops a start, ``throttle``
  slows starts, ``reorder`` changes who goes first).
* :func:`tick` is the capture one dispatcher tick runs inside (``kanban_candidate_receipts.dispatch_pass``). The
  few decisions a tick makes without a ``DispatchResult`` bucket report here: rows ``lane_rows`` filtered out, the
  host-slot census, a full admission pool, the tail behind a spent spawn budget.
* :func:`record_tick` (the post-result seam, ``kanban_capacity_evidence.observe_dispatch``) turns that capture and
  the tick's ``DispatchResult`` into the board's snapshot: every idle card charged to exactly ONE governor (the
  first that stopped it, in dispatcher order), every other governor that also holds it listed as an overlap, and
  per governor the reading, threshold, since and cards charged. Charges sum to the idle total. It is written to
  ``<board db>.governors.json`` beside the board DB: machine-local, never synced, no board write.
* :func:`snapshot` reads it back. A consumer reads what the dispatcher decided; it does not re-derive it.

The first stop always comes from the tick. Overlaps cannot: the dispatcher stops at the first refusal, so the rules
after it are asked afterwards (``kanban_governors_explain.card_chain``, the same predicate functions, read-only),
inside a small per-tick time budget; a card the budget did not reach has ``overlaps: null`` (not evaluated), never
an empty list.

Advisory like every notice: nothing here is read by admission, and a fault never stops a tick.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

_log = logging.getLogger(__name__)

VERSION = 1
SUFFIX = ".governors.json"
MAX_CARDS = 2000  # per-card rows kept in one snapshot; counts stay complete past it
MAX_IDS = 25  # card ids listed on one governor row
OVERLAP_BUDGET_SECONDS = 0.3  # per tick, for asking the rules after each card's first stop
OVERLAP_TTL_SECONDS = 300  # an evaluated overlap list is reused this long while the first stop is unchanged
UNCHANGED_WRITE_SECONDS = 10  # an unchanged snapshot is rewritten (fresh ``tick_at``) at most this often
STALE_SECONDS = 180  # a snapshot older than this is reported stale: no dispatcher is ticking this board

HOLD, THROTTLE, REORDER = "hold", "throttle", "reorder"


@dataclass(frozen=True)
class Governor:
    id: str
    label: str
    scope: str
    source: str
    threshold: str
    kind: str = HOLD


# Dispatcher order: ``_dispatch_once_locked`` top to bottom, then ``_dispatch_lane_task``, then the claim
# transaction. A card's first stop is the earliest entry here that refused it.
_REGISTRY: tuple[Governor, ...] = (
    Governor("gateway_drain", "Gateway drain", "host", "hermes_cli/kanban_gateway_drain.py:dispatch_paused",
             "gateway_state.json gateway_state=draining / restart_requested"),
    Governor("core_update_drain", "Core-update drain", "host", "hermes_cli/kanban_gateway_drain.py:core_update_hold",
             "agent.core_update_drain hold file"),
    Governor("max_spawn", "Board spawn cap", "board", "hermes_cli/kanban_db_dispatch.py:_tick_spawn_budget",
             "kanban.max_spawn"),
    Governor("host_slots", "Host slots", "host", "hermes_cli/kanban_admission_pools.py:host_remaining",
             "kanban.max_in_progress"),
    Governor("board_share", "Board share", "host", "hermes_cli/kanban_board_share.py:shares",
             "kanban.max_in_progress (free slots ranked across boards)", REORDER),
    Governor("memory_pressure", "Memory pressure", "host", "hermes_cli/kanban_db_dispatch.py:_memory_pressure_level",
             "gateway.memory_status.classify_pressure (critical: 0 starts, elevated: 1)", THROTTLE),
    Governor("containment", "Containment", "card", "hermes_cli/kanban_containment_connections.py:held",
             "pending containment episode"),
    Governor("lane_paused", "Lane paused", "lane", "hermes_cli/kanban_lanes.py:drop_paused",
             "kanban.admission.lanes.<lane>.paused"),
    Governor("lane_max_workers", "Lane worker cap", "lane", "hermes_cli/kanban_lane_workers.py:drop_full",
             "kanban.admission.lanes.<lane>.max_workers"),
    Governor("lane_share", "Lane share order", "lane", "hermes_cli/kanban_lanes.py:order",
             "kanban.admission.lanes.<lane>.share", REORDER),
    Governor("hub_claim_screen", "Hub claim screen", "card", "hermes_cli/kanban_hub_admission_claim.py:screen",
             "kanban_hub_admission_claim.MAX_JUDGED_PER_TICK", THROTTLE),
    Governor("placement", "Labels / pickup", "machine", "hermes_cli/kanban_worker_node.py:dispatch_refusals",
             "tasks.runs_on against this machine's labels"),
    Governor("admission_pool", "Admission pool", "host", "hermes_cli/kanban_admission_pools.py:pool_admits",
             "kanban.admission priority slots (marked-only pool)"),
    Governor("unassigned", "Unassigned", "card", "hermes_cli/kanban_db_default_assignee.py:fill_default_assignee",
             "kanban.default_assignee"),
    Governor("nonspawnable", "No such profile", "profile",
             "hermes_cli/kanban_db_dispatch_admission.py:lane_admission_refused", "kanban.dispatch_profiles"),
    Governor("capsule", "Capsule transport", "card", "hermes_cli/kanban_capsule_admission.py:dispatch_refusal",
             "registered capsule transport for host_repo cards"),
    Governor("refusal_fallback", "Safety refusals", "card", "hermes_cli/kanban_refusal_fallback.py:admission",
             "every eligible provider refused the card"),
    Governor("class_walls", "Class walls", "provider", "hermes_cli/kanban_class_walls.py:unroutable_class",
             "model_routing.routes (class has no usable route)"),
    Governor("review_provider_wall", "Review provider wall", "provider", "hermes_cli/kanban_review_admission.py:hold",
             "review route quota walls"),
    Governor("no_progress", "No-progress budget", "card", "hermes_cli/kanban_no_progress.py:hold",
             "kanban.no_progress_runs"),
    Governor("spend_budget", "Spend budget", "card", "hermes_cli/kanban_spend_budget.py:hold",
             "kanban.spend_budget_runs / kanban.spend_budget_cache_read_tokens"),
    Governor("provider_walls", "Provider walls", "provider", "hermes_cli/kanban_dispatch_skip.py:pinned_quota_hold",
             "kanban_provider_walls.active_walls / live_quota_wall"),
    Governor("profile_cap", "Per-profile cap", "profile", "hermes_cli/kanban_db_dispatch.py:_dispatch_lane_task",
             "kanban.max_in_progress_per_profile"),
    Governor("model_pin", "Model pin", "card", "hermes_cli/kanban_model_assignment.py:route_model_mismatch",
             "model_routing registry against the card's pin"),
    Governor("respawn_guard", "Respawn guard", "card", "hermes_cli/kanban_db_dispatch.py:check_respawn_guard",
             "rate-limit / infrastructure / owner-restart cooldowns, recent_success, active_pr"),
    Governor("llm_pool", "LLM pool", "pool", "hermes_cli/kanban_llm_pacing_dispatch.py:hold",
             "kanban.llm_pacing.reserve_seven_day_percent / reserve_five_hour_percent (every route at reserve)"),
    Governor("ratio_routing", "Ratio routing", "provider", "hermes_cli/kanban_ratio_routing.py:pick",
             "llm_pacing.json v2.pools[p].ratio", REORDER),
    Governor("start_gate", "Plugin start gate", "host", "hermes_cli/kanban_start_gate.py:hold",
             "kanban_start_gate plugin hook"),
    Governor("owner_down", "Owner down", "profile", "hermes_cli/kanban_owner_down.py:probe",
             "the assignee's gateway service is failed / masked / disabled"),
    Governor("quiet", "Claim quiet", "host", "hermes_cli/kanban_db_dispatch_admission.py:quiet_claim_check",
             "agent.claim_quiet lease"),
    Governor("update_fence", "Update fence", "host", "hermes_cli/kanban_admission.py:admission_hold_set",
             "<root>/core-update/holds/hermes-sync"),
    Governor("disk_floor", "Disk floor", "host", "hermes_cli/kanban_disk_admission.py:refusal_reason",
             "kanban.admission.disk_free_floor_bytes"),
    Governor("worker_resources", "Worker resources", "host", "hermes_cli/worker_resource_admission.py:refusal",
             "worker compute capacity (pool occupancy, pressure)"),
    Governor("hub_cpu", "Hub CPU", "host", "hermes_cli/kanban_admission.py:hub_cpu_advisory",
             "kanban.admission.cpu_busy_below_pct"),
    Governor("board_lease", "Board lease", "card", "hermes_cli/kanban_dispatch_verdict.py:claim_refused",
             "the Board's claim lease (lease_held / offline / claim_not_acquired)"),
    Governor("spawn_failed", "Spawn failed", "card", "hermes_cli/kanban_failure_accounting.py:_record_task_failure",
             "kanban.failure_limit (retried until the breaker blocks the card)"),
    # Not dispatcher-tick rules: where a card that is not on a dispatch lane waits.
    Governor("lane_gate", "Lane gate", "lane", "hermes_cli/kanban_lanes.py:held",
             "kanban.admission.lanes.<lane>.gated / admit_per_day"),
    Governor("lane_daily_limit", "Lane daily limit", "lane", "hermes_cli/kanban_lane_daily_limits.py:reason",
             "kanban.admission.lanes.<lane>.cards_per_day / usd_per_day"),
    Governor("dependencies", "Open parents", "card", "hermes_cli/kanban_db.py:recompute_ready",
             "every parent done or archived"),
    Governor("schedule", "Scheduled wait", "card", "hermes_cli/kanban_continuation.py:wake_due_continuations",
             "the park's wake time or wake_check"),
    Governor("unrecorded", "Unrecorded", "card", "hermes_cli/kanban_dispatch_skip.py:unrecorded",
             "a tick passed the card over without recording why"),
)
_BY_ID = {g.id: g for g in _REGISTRY}
_ORDER = {g.id: index for index, g in enumerate(_REGISTRY)}
# The caps one tick's spawn budget is the minimum of, in ``_tick_spawn_budget`` order (a tie keeps the earlier).
_BUDGET_ORDER = ("max_spawn", "host_slots", "board_share", "memory_pressure")


def registry() -> list[dict[str, str]]:
    """Every governor, in dispatcher order."""
    return [{"id": g.id, "label": g.label, "scope": g.scope, "source": g.source, "threshold": g.threshold,
             "kind": g.kind} for g in _REGISTRY]


def governor(governor_id: str) -> Optional[Governor]:
    return _BY_ID.get(governor_id)


def order_of(governor_id: str) -> int:
    return _ORDER.get(governor_id, len(_REGISTRY))


# --- one tick's capture ----------------------------------------------------------------------------------------


@dataclass
class Tick:
    caps: dict[str, Any]
    host: dict[str, Any] = field(default_factory=dict)
    charges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


_current: ContextVar[Optional[Tick]] = ContextVar("kanban_governor_tick", default=None)
_finished = threading.local()


@contextlib.contextmanager
def tick(conn: Any, caps: Optional[dict[str, Any]] = None) -> Iterator[Tick]:
    """Capture one dispatcher tick; :func:`record_tick` picks the finished capture up by connection."""
    capture = Tick(dict(caps or {}))
    token = _current.set(capture)
    try:
        yield capture
    finally:
        _current.reset(token)
        _finished.capture = (id(conn), capture)


def charge(task_id: str, governor_id: str, scope: Optional[str] = None, **detail: Any) -> None:
    """A tick decision with no ``DispatchResult`` bucket: ``governor_id`` stopped ``task_id``. No-op outside a tick."""
    capture = _current.get()
    if capture is not None:
        capture.charges.setdefault(task_id, []).append({"governor": governor_id, "scope": scope, **detail})


def dropped(before: list, after: list, governor_id: str) -> list:
    """``lane_rows`` seam: the rows a filter removed are charged to its governor. Returns ``after``."""
    if _current.get() is not None and len(after) != len(before):
        kept = {row["id"] for row in after}
        for row in before:
            if row["id"] not in kept:
                charge(row["id"], governor_id)
    return after


def tail(rows: list, reason: str) -> None:
    """Rows behind a spent spawn budget; :func:`record_tick` names the cap that set the budget."""
    for row in rows:
        charge(row["id"], "_budget", reason=reason)


def host_use(cap: Any, own: Any, others: Any) -> None:
    """``host_remaining``'s census: the cap, this board's running workers and every other board's."""
    capture = _current.get()
    if capture is not None:
        capture.host.update(cap=cap, own=own, others=others)


def board_use(counts: dict[str, int]) -> None:
    """``count_running_tasks_other_boards``'s per-board running workers, as that census read them."""
    capture = _current.get()
    if capture is not None:
        capture.host["boards"] = dict(counts)


def _take_capture(conn: Any) -> Optional[Tick]:
    done = getattr(_finished, "capture", None)
    _finished.capture = None
    return done[1] if done is not None and done[0] == id(conn) else None


# --- from one tick's result to charges ---------------------------------------------------------------------------


def _capacity(text: Any) -> tuple[Optional[int], Optional[int]]:
    """``(used, limit)`` out of a ``DispatchResult.at_capacity`` string (``max_in_progress=2/2``)."""
    import re

    found = re.search(r"=(\d+)/(\d+)", str(text or ""))
    return (int(found.group(1)), int(found.group(2))) if found else (None, None)


def _host_detail(capture: Optional[Tick], board: Optional[str]) -> dict[str, Any]:
    host = capture.host if capture is not None else {}
    if not host:
        return {}
    boards = dict(host.get("boards") or {})
    if host.get("own") is not None:
        boards[board or "default"] = host["own"]
    return {"boards": boards} if boards else {}


def global_stop(result: Any, capture: Optional[Tick], *, board: Optional[str],
                update_drain: bool = False) -> Optional[dict[str, Any]]:
    """The governor that kept this whole tick from scheduling, or None when it scheduled.

    The dispatcher returns before scheduling in exactly four places: the gateway is draining, a core-update hold is
    live, a concurrency cap is full (``at_capacity``) or memory pressure is critical. ``update_drain`` is the
    gateway's own core-update hold, which it enforces as ``max_spawn=0``.
    """
    if getattr(result, "scheduling_admitted", False):
        return None
    if getattr(result, "core_update_drain_hold", None) is not None or update_drain:
        hold = getattr(result, "core_update_drain_hold", None) or {}
        return {"governor": "core_update_drain", "detail": {k: hold.get(k) for k in ("pid", "path") if hold.get(k)}}
    capacity = getattr(result, "at_capacity", None)
    if capacity:
        used, limit = _capacity(capacity)
        text = str(capacity)
        if text.startswith("max_spawn"):
            return {"governor": "max_spawn", "reading": used, "threshold": limit, "detail": {"at_capacity": text}}
        if text.startswith("board_share"):
            return {"governor": "board_share", "reading": 0, "threshold": None, "detail": {"at_capacity": text}}
        return {"governor": "host_slots", "reading": used, "threshold": limit,
                "detail": {"at_capacity": text, **_host_detail(capture, board)}}
    if getattr(result, "memory_pressure", None) == "critical":
        return {"governor": "memory_pressure", "reading": "critical", "threshold": "critical"}
    return {"governor": "gateway_drain"}


def _budget_cap(result: Any, capture: Optional[Tick], *, board: Optional[str], running: int) -> dict[str, Any]:
    """Which cap set this tick's spawn budget (the smallest; a tie keeps ``_tick_spawn_budget``'s earlier one)."""
    caps = capture.caps if capture is not None else {}
    host = capture.host if capture is not None else {}
    started = len(getattr(result, "spawned", ()) or ())
    own = host.get("own") if host.get("own") is not None else max(0, running - started)
    budgets: dict[str, tuple[int, dict[str, Any]]] = {}
    if isinstance(caps.get("max_spawn"), int):
        budgets["max_spawn"] = (caps["max_spawn"] - own, {"reading": own + started, "threshold": caps["max_spawn"]})
    if isinstance(caps.get("max_in_progress"), int):
        others = host.get("others") or 0
        budgets["host_slots"] = (caps["max_in_progress"] - own - others, {
            "reading": own + others + started, "threshold": caps["max_in_progress"],
            "detail": _host_detail(capture, board)})
    if isinstance(caps.get("spawn_quota"), int):
        budgets["board_share"] = (caps["spawn_quota"], {"reading": started, "threshold": caps["spawn_quota"]})
    if getattr(result, "memory_pressure", None) == "elevated":
        budgets["memory_pressure"] = (1, {"reading": "elevated", "threshold": "elevated"})
    if not budgets:
        return {"governor": "unrecorded", "detail": {"reason": "spawn budget exhausted, no cap recorded"}}
    name = min(_BUDGET_ORDER, key=lambda key: budgets[key][0] if key in budgets else float("inf"))
    return {"governor": name, **budgets[name][1]}


def _guard_charge(reason: str) -> Optional[dict[str, Any]]:
    """A ``respawn_guarded`` reason as a charge; None for the pacing hold (``llm_paced`` carries it)."""
    from hermes_cli.kanban_llm_pacing import GUARD_REASON
    from hermes_cli.kanban_review_admission import REASON as REVIEW_WALL
    from hermes_cli.kanban_start_gate import word

    if reason == GUARD_REASON:
        return None
    if word(reason):
        return {"governor": "start_gate", "scope": word(reason), "detail": {"reason": word(reason)}}
    if reason in (REVIEW_WALL, "review_model_unavailable"):
        return {"governor": "review_provider_wall", "detail": {"reason": reason}}
    if reason.startswith("workspace_"):  # ``kanban_source_launch``: the claim was released, the card retries
        return {"governor": "spawn_failed", "detail": {"reason": reason}}
    if reason.startswith("model_"):  # ``ModelProviderMismatchError`` codes (kanban_model_assignment)
        return {"governor": "model_pin", "detail": {"reason": reason}}
    return {"governor": "respawn_guard", "detail": {"reason": reason}}


def _ineligible_charge(reason: Any) -> dict[str, Any]:
    text = str(reason)
    if text.startswith("model_unroutable:"):
        return {"governor": "class_walls", "detail": {"reason": text.split(":", 1)[1]}}
    from hermes_cli.kanban_refusal_fallback import EXHAUSTED

    if text == EXHAUSTED:
        return {"governor": "refusal_fallback", "detail": {"reason": text}}
    return {"governor": "placement", "detail": {"reason": text}}


_CLAIM_GATES = (("hub_cpu", "hub_cpu"), ("hub_admission", "hub_cpu"), ("hub_capacity", "host_slots"),
                ("worker_resources", "worker_resources"), ("worker_disk", "disk_floor"), ("disk", "disk_floor"),
                ("update_admission_hold", "update_fence"), ("labels_mismatch", "placement"),
                ("pinned_only", "placement"), ("containment", "containment"), ("capacity", "host_slots"))


def _claim_charge(code: Any) -> dict[str, Any]:
    """A ``claim_deferred`` code as a charge: this host's admission gates by name, else the Board's lease."""
    text = str(code)
    name = next((gov for prefix, gov in _CLAIM_GATES if text.startswith(prefix)), "board_lease")
    return {"governor": name, "detail": {"reason": text, **({"origin": code.origin}
                                                             if getattr(code, "origin", None) else {})}}


def result_charges(result: Any) -> dict[str, list[dict[str, Any]]]:
    """``task_id -> charges`` for every row the tick's ``DispatchResult`` says it passed over."""
    out: dict[str, list[dict[str, Any]]] = {}

    def add(task_id: str, entry: Optional[dict[str, Any]]) -> None:
        if entry is not None:
            out.setdefault(task_id, []).append(entry)

    for tid in getattr(result, "admission_deferred", ()):
        add(tid, {"governor": "hub_claim_screen"})
    for tid, reason in getattr(result, "skipped_ineligible", ()):
        add(tid, _ineligible_charge(reason))
    for tid in getattr(result, "skipped_unassigned", ()):
        add(tid, {"governor": "unassigned"})
    for tid in getattr(result, "skipped_nonspawnable", ()):
        add(tid, {"governor": "nonspawnable"})
    for tid, code in getattr(result, "capsule_refused", ()):
        add(tid, {"governor": "capsule", "detail": {"reason": str(code)}})
    for tid, action in getattr(result, "no_progress_held", ()):
        spend = str(action).startswith("spend_budget:")
        add(tid, {"governor": "spend_budget" if spend else "no_progress", "detail": {"action": str(action)}})
    for tid, model, provider, until in getattr(result, "pinned_quota_held", ()):
        add(tid, {"governor": "provider_walls", "scope": str(provider), "until": until,
                  "detail": {"model": model, "provider": provider}})
    for tid, who, running in getattr(result, "skipped_per_profile_capped", ()):
        add(tid, {"governor": "profile_cap", "scope": str(who), "reading": running})
    for tid, reason in getattr(result, "respawn_guarded", ()):
        add(tid, _guard_charge(str(reason)))
    for tid, detail, until, extra in getattr(result, "llm_paced", ()):
        pools = [p.get("provider") for p in (extra or {}).get("providers", ()) if isinstance(p, dict)]
        add(tid, {"governor": "llm_pool", "until": until,  # one pool governor; the card's own routes are detail
                  "detail": {"card_routes_at_reserve": pools, "held": detail}})
    for tid, who, code in getattr(result, "owner_down", ()):
        add(tid, {"governor": "owner_down", "scope": str(who), "detail": {"reason": str(code)}})
    for tid, episode in getattr(result, "quiet_deferred", ()):
        add(tid, {"governor": "quiet", "detail": {"episode": str(episode)}})
    for tid, code in getattr(result, "claim_deferred", ()):
        add(tid, _claim_charge(code))
    return out


# --- readings ----------------------------------------------------------------------------------------------------


def pool_reading() -> dict[str, Any]:
    """The LLM pool (every governed provider's accounts together) as this home's dispatchers stored it.

    ``reading``: ``used_pct`` is the mean, over every account read, of that account's used percentage on its
    binding window (the one closest to, or furthest past, its reserve); ``accounts_under_reserve`` of ``accounts``
    still have room. ``threshold``: the reserve per window. ``detail.providers`` carries the same numbers per
    provider. A card is stopped only when every provider it can route to is at reserve (``hold``), so the pool
    can show room while a card pinned to one exhausted provider waits.
    """
    from hermes_cli import kanban_llm_pacing_dispatch as pacing

    cfg = pacing.load_config()
    state = pacing._hold_state()
    providers: dict[str, dict[str, Any]] = {}
    used_all: list[float] = []
    under_all = 0
    for provider in cfg.providers or ():
        pool = pacing._v2_pool(state, provider) or {}
        listed = pool.get("accounts") if isinstance(pool.get("accounts"), list) else []
        binding: dict[Any, tuple[float, float]] = {}  # account -> (margin to reserve, used) on its binding window
        for account in listed:
            if not isinstance(account, dict) or not isinstance(account.get("used"), (int, float)):
                continue
            margin = float(account["used"]) - float(cfg.reserve.get(str(account.get("kind")), 100.0))
            if account.get("id") not in binding or margin > binding[account.get("id")][0]:
                binding[account.get("id")] = (margin, float(account["used"]))
        used = [value for _margin, value in binding.values()]
        under = sum(1 for margin, _value in binding.values() if margin < 0)
        used_all += used
        under_all += under
        providers[provider] = {"accounts": len(used), "accounts_under_reserve": under,
                               "used_pct": round(sum(used) / len(used), 1) if used else None,
                               "at_reserve": pool.get("at_reserve"), "until": pool.get("until"),
                               "read_at": pool.get("read_at")}
    return {"reading": {"used_pct": round(sum(used_all) / len(used_all), 1) if used_all else None,
                        "accounts_under_reserve": under_all, "accounts": len(used_all)},
            "threshold": {kind: float(value) for kind, value in cfg.reserve.items()},
            "detail": {"providers": providers}}


def _lane_reading(conn: Any, pol: Any, lane_name: str, governor_id: str, *, now: float) -> dict[str, Any]:
    from hermes_cli import kanban_lanes as lanes

    lane = pol.lanes[pol.lane_of(lane_name)]
    if governor_id == "lane_gate":
        return {"reading": lanes.admitted_today(conn, lane.name, now=now), "threshold": lane.admit_per_day,
                "detail": {"unit": "admitted today / admit_per_day"}}
    if governor_id == "lane_max_workers":
        return {"reading": lanes._running(conn, pol).get(lane.name), "threshold": lane.max_workers}
    if governor_id == "lane_daily_limit":
        if lane.cards_per_day is not None:
            return {"reading": lanes.cards_today(conn, lane.name, now=now, pol=pol), "threshold": lane.cards_per_day,
                    "detail": {"unit": "cards today / cards_per_day"}}
        return {"reading": lanes.spent_today(conn, pol, lane.name, now=now), "threshold": lane.usd_per_day,
                "detail": {"unit": "usd today / usd_per_day"}}
    return {"detail": {"paused": lane.paused}}


def _fill_reading(conn: Any, entry: dict[str, Any], tenant: Any, capture: Optional[Tick], *, now: float,
                  memo: dict) -> dict[str, Any]:
    """Add the scope and the reading/threshold a charge did not carry. Reads are memoized per tick."""
    name = entry["governor"]
    key: Any = None
    try:
        if name.startswith("lane_"):
            from hermes_cli import kanban_lanes as lanes

            pol = memo.setdefault("policy", lanes.safe_policy())
            lane = pol.lane_of(tenant) if pol is not None else (tenant or lanes.DEFAULT_LANE)
            entry["scope"] = lane
            key = (name, lane)
            if key not in memo:
                memo[key] = _lane_reading(conn, pol, lane, name, now=now) if pol is not None else {}
        elif name == "llm_pool":
            key = (name, None)
            if key not in memo:
                memo[key] = pool_reading()
        elif name == "profile_cap" and capture is not None:
            entry.setdefault("threshold", capture.caps.get("max_in_progress_per_profile"))
    except Exception:  # health: allow BLE001 -- a reading is evidence; the charge itself already stands
        _log.debug("kanban governors: reading unavailable for %s", name, exc_info=True)
        memo[key] = {}
    for field_name, value in (memo.get(key) or {}).items() if key is not None else ():
        if field_name == "detail":
            entry["detail"] = {**value, **(entry.get("detail") or {})}
        else:
            entry.setdefault(field_name, value)
    return entry


# --- the snapshot ----------------------------------------------------------------------------------------------


def _db_path(conn: Any) -> Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2])
    raise ValueError("kanban governors: the board connection has no on-disk database")


def snapshot_path(conn: Any) -> Path:
    path = _db_path(conn)
    return path.with_name(path.name + SUFFIX)


def _read(path: Path) -> Optional[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("version") == VERSION else None


def _write(path: Path, doc: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(doc, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _board(conn: Any) -> str:
    from hermes_cli.kanban_capacity_evidence import _board as board_of

    return board_of(conn)


def _lane_cards(conn: Any) -> list[tuple[str, str, Any]]:
    """``(id, status, tenant)`` of every unclaimed card on a dispatch lane (review only when it dispatches)."""
    from hermes_cli.kanban_db_dispatch import review_dispatch_enabled

    statuses = ("ready", "review") if review_dispatch_enabled() else ("ready",)
    marks = ",".join("?" for _ in statuses)
    return [(row[0], row[1], row[2]) for row in conn.execute(
        f"SELECT id, status, tenant FROM tasks WHERE status IN ({marks}) AND claim_lock IS NULL "
        "ORDER BY created_at, id", statuses)]


def _gate_cards(conn: Any) -> list[tuple[str, str, Any, str]]:
    """``(id, 'triage', tenant, governor)`` for cards waiting at a lane gate, told apart by why they were gated."""
    from hermes_cli import kanban_lanes as lanes

    out = []
    for row in lanes.held(conn):
        event = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                             (row["id"], lanes.GATED)).fetchone()
        try:
            reason = (json.loads(event[0] or "{}") if event else {}).get("reason")
        except ValueError:
            reason = None
        limited = isinstance(reason, str) and reason.startswith("limit:")
        out.append((row["id"], "triage", row["tenant"], "lane_daily_limit" if limited else "lane_gate"))
    return out


def _card_charges(tid: str, stop: Optional[dict], capture: Optional[Tick], by_result: dict, budget: dict) -> list[dict]:
    """Every charge the tick made against one lane card, in dispatcher order (the first is its first stop)."""
    found = ([dict(stop)] if stop is not None else []) + [dict(c) for c in by_result.get(tid, ())]
    for entry in (capture.charges.get(tid, ()) if capture is not None else ()):
        found.append({**budget, "detail": {**(budget.get("detail") or {}), "reason": entry.get("reason")}}
                     if entry["governor"] == "_budget" else dict(entry))
    if not found:
        found.append({"governor": "unrecorded"})
    found.sort(key=lambda entry: order_of(entry["governor"]))
    unique, seen = [], set()
    for entry in found:
        key = (entry["governor"], entry.get("scope"))
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    return unique


def _with_since(entry: dict[str, Any], before: Optional[dict[str, Any]], now: float) -> dict[str, Any]:
    same = isinstance(before, dict) and before.get("governor") == entry["governor"] \
        and before.get("scope") == entry.get("scope") and isinstance(before.get("since"), (int, float))
    entry["since"] = before["since"] if same else int(now)
    return entry


def _overlaps(conn: Any, cards: dict[str, dict], previous: dict[str, Any], now: float) -> None:
    """Ask the rules after each lane card's first stop what else holds it, inside the tick's budget."""
    from hermes_cli import kanban_governors_explain as explain

    deadline = time.monotonic() + OVERLAP_BUDGET_SECONDS
    env = None
    asked = 0
    for tid, card in cards.items():
        if card["status"] not in ("ready", "review"):
            card["overlaps"] = card["tick_overlaps"]
            continue
        before = previous.get(tid) if isinstance(previous.get(tid), dict) else {}
        fresh = isinstance(before.get("overlaps_at"), (int, float)) and now - before["overlaps_at"] < OVERLAP_TTL_SECONDS
        if fresh and before.get("governor") == card["governor"] and isinstance(before.get("overlaps"), list):
            card["overlaps"], card["overlaps_at"] = before["overlaps"], before["overlaps_at"]
            continue
        if asked and time.monotonic() >= deadline:  # at least one card a tick, so a slow host still converges
            card["overlaps"] = None  # not evaluated this tick: never read as "no overlap"
            continue
        asked += 1
        try:
            env = explain.environment(conn) if env is None else env
            chain = explain.card_chain(conn, tid, env, after=card["governor"])
        except Exception:  # health: allow BLE001 -- overlaps are evidence; the first stop already stands
            _log.debug("kanban governors: overlap check failed for %s", tid, exc_info=True)
            card["overlaps"] = None
            continue
        seen = {(card["governor"], card.get("scope"))} | {(o["governor"], o.get("scope")) for o in card["tick_overlaps"]}
        extra = [{k: step[k] for k in ("governor", "scope", "reading", "threshold", "detail") if step.get(k) is not None}
                 for step in chain if step["verdict"] == "stop" and (step["governor"], step.get("scope")) not in seen
                 and step["governor"] != card["governor"]]
        card["overlaps"], card["overlaps_at"] = card["tick_overlaps"] + extra, int(now)


def _gauges(conn: Any, capture: Optional[Tick], *, board: str, now: float) -> list[dict[str, Any]]:
    """Readings of the capacity governors whether or not they hold a card (an inactive row still shows use)."""
    out: list[dict[str, Any]] = []
    host = capture.host if capture is not None else {}
    if host.get("cap") is not None and host.get("own") is not None:
        out.append({"governor": "host_slots", "scope": None, "reading": host["own"] + (host.get("others") or 0),
                    "threshold": host["cap"], "detail": _host_detail(capture, board)})
    try:
        from hermes_cli import kanban_llm_pacing_dispatch as pacing

        if pacing.load_config().enabled:
            out.append({"governor": "llm_pool", "scope": None, **pool_reading()})
    except Exception:  # health: allow BLE001 -- a gauge is evidence only
        _log.debug("kanban governors: pacing gauge unavailable", exc_info=True)
    try:
        from hermes_cli import kanban_lanes as lanes

        pol = lanes.enforced()
        for name, lane in (pol.lanes.items() if pol is not None else ()):
            if lane.gated:
                out.append({"governor": "lane_gate", "scope": name, **_lane_reading(conn, pol, name, "lane_gate", now=now)})
            if lane.max_workers is not None:
                out.append({"governor": "lane_max_workers", "scope": name,
                            **_lane_reading(conn, pol, name, "lane_max_workers", now=now)})
    except Exception:  # health: allow BLE001 -- a gauge is evidence only
        _log.debug("kanban governors: lane gauge unavailable", exc_info=True)
    return out


def _governor_rows(cards: dict[str, dict], counts: dict[str, int], gauges: list[dict], previous: list, now: float) -> list:
    rows: dict[tuple, dict[str, Any]] = {}
    for gauge in gauges:
        rows[(gauge["governor"], gauge.get("scope"))] = {**gauge, "cards": 0, "card_ids": []}
    for tid, card in cards.items():
        row = rows.setdefault((card["governor"], card.get("scope")), {
            "governor": card["governor"], "scope": card.get("scope"), "cards": 0, "card_ids": []})
        row["cards"] += 1
        if len(row["card_ids"]) < MAX_IDS:
            row["card_ids"].append(tid)
        for key in ("reading", "threshold", "until", "detail"):
            if row.get(key) is None and card.get(key) is not None:
                row[key] = card[key]
    for name, count in counts.items():
        if count:
            rows[(name, None)] = {"governor": name, "scope": None, "cards": count, "card_ids": []}
    before = {(r.get("governor"), r.get("scope")): r for r in previous if isinstance(r, dict)}
    out = []
    for key, row in rows.items():
        known = _BY_ID.get(row["governor"])
        row.update(label=known.label if known else row["governor"], kind=known.kind if known else HOLD,
                   active=row["cards"] > 0)
        prior = before.get(key) or {}
        row["since"] = (prior.get("since") if prior.get("active") and prior.get("since") else int(now)) \
            if row["active"] else None
        out.append(row)
    out.sort(key=lambda r: (order_of(r["governor"]), str(r.get("scope") or "")))
    return out


def build(conn: Any, result: Any, capture: Optional[Tick], *, board: str, update_drain: bool = False,
          previous: Optional[dict[str, Any]] = None, now: Optional[float] = None) -> dict[str, Any]:
    """The board's snapshot for one finished tick (see the module docstring for the charging rule)."""
    from hermes_cli.kanban_db_dispatch import count_running_tasks

    now = time.time() if now is None else now
    previous = previous or {}
    before = previous.get("cards") if isinstance(previous.get("cards"), dict) else {}
    stop = global_stop(result, capture, board=board, update_drain=update_drain)
    budget = _budget_cap(result, capture, board=board, running=count_running_tasks(conn))
    by_result = result_charges(result)
    memo: dict = {}
    cards: dict[str, dict[str, Any]] = {}
    listed = [(tid, status, tenant, None) for tid, status, tenant in _lane_cards(conn)] + _gate_cards(conn)
    for tid, status, tenant, gate in listed:
        charges = [{"governor": gate}] if gate else _card_charges(tid, stop, capture, by_result, budget)
        charges = [_fill_reading(conn, entry, tenant, capture, now=now, memo=memo) for entry in charges]
        first = _with_since({**charges[0], "status": status}, before.get(tid), now)
        first["tick_overlaps"] = [{k: v for k, v in entry.items() if v is not None} for entry in charges[1:]]
        cards[tid] = first
    counts = {name: int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)).fetchone()[0])
              for name, status in (("dependencies", "todo"), ("schedule", "scheduled"))}
    idle_total = len(cards) + sum(counts.values())
    kept = dict(list(cards.items())[:MAX_CARDS])
    _overlaps(conn, kept, before, now)
    governors = _governor_rows(cards, counts, _gauges(conn, capture, board=board, now=now),
                               previous.get("governors") if isinstance(previous.get("governors"), list) else [], now)
    for card in kept.values():
        card.pop("tick_overlaps", None)
    return {"version": VERSION, "board": board, "tick_at": now, "idle_total": idle_total,
            "caps": dict(capture.caps) if capture is not None else {},  # the limits this tick ran with
            "charged_total": sum(row["cards"] for row in governors), "truncated": len(cards) > len(kept),
            "scheduling_admitted": bool(getattr(result, "scheduling_admitted", False)),
            "global_stop": stop["governor"] if stop else None, "governors": governors, "cards": kept}


def _same(doc: dict[str, Any], previous: Optional[dict[str, Any]]) -> bool:
    if previous is None:
        return False
    strip = lambda d: {k: v for k, v in d.items() if k != "tick_at"}
    return strip(doc) == strip(previous)


def record_tick(conn: Any, result: Any, *, dry_run: bool = False, update_drain: bool = False) -> Optional[dict]:
    """Write the snapshot of one finished tick; returns it, or None when the tick decided nothing. Never raises."""
    capture = _take_capture(conn)
    if dry_run or getattr(result, "skipped_locked", False) or getattr(result, "skipped_owned", False) \
            or getattr(result, "storage_skip_reason", None):
        return None
    try:
        path = snapshot_path(conn)
        previous = _read(path)
        doc = build(conn, result, capture, board=_board(conn), update_drain=update_drain, previous=previous)
        age = doc["tick_at"] - float(previous.get("tick_at") or 0) if previous else None
        if not (_same(doc, previous) and age is not None and age < UNCHANGED_WRITE_SECONDS):
            _write(path, doc)
        return doc
    except Exception:  # health: allow BLE001 -- the tick already happened; its evidence is advisory
        _log.debug("kanban governors: snapshot failed", exc_info=True)
        return None


def snapshot(conn: Any, *, now: Optional[float] = None) -> dict[str, Any]:
    """The board's governor state as its dispatcher last recorded it, with every registry governor present.

    ``available`` False = no tick recorded one here yet; ``stale`` True = the newest is older than
    :data:`STALE_SECONDS`. Neither reads as "nothing is held": a consumer must show them as unknown.
    """
    now = time.time() if now is None else now
    doc = _read(snapshot_path(conn))
    board = _board(conn)
    if doc is None:
        return {"version": VERSION, "board": board, "available": False, "stale": True, "age_s": None,
                "governors": _all_governors([]), "cards": {}, "idle_total": None, "charged_total": None}
    age = max(0.0, now - float(doc.get("tick_at") or 0))
    return {**doc, "available": True, "age_s": round(age, 1), "stale": age > STALE_SECONDS,
            "governors": _all_governors(doc.get("governors") or [])}


def _all_governors(rows: list) -> list[dict[str, Any]]:
    seen = {row.get("governor") for row in rows}
    out = list(rows) + [{"governor": g.id, "scope": None, "label": g.label, "kind": g.kind, "active": False,
                         "cards": 0, "card_ids": [], "since": None} for g in _REGISTRY if g.id not in seen]
    out.sort(key=lambda r: (order_of(r["governor"]), str(r.get("scope") or "")))
    return out


def card(conn: Any, task_id: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """One card's recorded first stop and overlaps (``age_s`` = how old that tick is), or None when the last
    recorded tick did not charge it (not idle then, past :data:`MAX_CARDS`, or no snapshot)."""
    now = time.time() if now is None else now
    doc = _read(snapshot_path(conn))
    found = (doc.get("cards") or {}).get(task_id) if doc else None
    if not isinstance(found, dict):
        return None
    age = max(0.0, now - float(doc.get("tick_at") or 0))
    known = _BY_ID.get(found.get("governor"))
    return {**found, "task_id": task_id, "board": doc.get("board"), "tick_at": doc.get("tick_at"),
            "age_s": round(age, 1), "stale": age > STALE_SECONDS, "label": known.label if known else None,
            "source": known.source if known else None}
