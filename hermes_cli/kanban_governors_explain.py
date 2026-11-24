"""``why`` for one card: the dispatcher's rules asked in dispatcher order, read-only. [daveey]

:func:`explain` walks the same path ``_dispatch_once_locked`` and ``_dispatch_lane_task`` walk for one card and
returns the ordered chain ``[{governor, verdict, reading, threshold, since, source, age_s}]`` plus ``first_stop``.
Every step CALLS the predicate the dispatcher calls (named in ``kanban_governors.registry()[..]["source"]``); none
re-implements one. Unlike a tick it does not stop at the first refusal, so the chain also shows what else would
hold the card once the first stop clears.

Read-only: the board connection is put in ``PRAGMA query_only`` for the walk, the hold functions are called with
``dry_run=True`` (the dispatcher's own no-write mode) or through the read half the hold itself calls first, and the
pacing governor judges the pools as stored (no usage read). A step that cannot be asked outside a real tick (the
hub claim screen's judge, the Board lease, the board-share quota, the worker-resources census) reads the last
recorded tick instead (``kanban_governors.card``): ``stop`` when that tick charged the card to it, else ``unknown``.
A predicate that raises is ``unknown`` with the error, never ``pass``.

Verdicts: ``pass``, ``stop`` (holds the start), ``throttle`` (slows starts, does not hold this one), ``unknown``.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Optional

from hermes_cli import kanban_governors as governors

PASS, STOP, THROTTLE, UNKNOWN = "pass", "stop", "throttle", "unknown"
LANES = ("ready", "review")
# Asked only inside a real tick: read from the last recorded one.
TICK_ONLY = ("hub_claim_screen", "admission_pool", "worker_resources", "board_lease", "board_share")


@dataclass
class Environment:
    """What one tick resolves once and every card's walk shares."""
    board: Optional[str]
    profile_exists: Optional[Callable[[str], bool]]
    default_assignee: Optional[str]
    per_profile_cap: Optional[int]
    max_spawn: Optional[int]
    max_in_progress: Optional[int]
    review_enabled: bool
    refusals: dict[str, dict[str, str]] = field(default_factory=dict)  # lane status -> dispatch_refusals
    memo: dict = field(default_factory=dict)


def environment(conn: Any) -> Environment:
    """The dispatcher's limits: the ones its last recorded tick on this board ran with (a gateway resolves them
    per tick, and ``hermes kanban dispatch --max N`` overrides config), else config read the way it reads it."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.config import load_config

    try:
        section = (load_config() or {}).get("kanban", {}) or {}
    except Exception:  # health: allow BLE001 -- an unreadable config is the dispatcher's "no caps" path too
        section = {}
    caps = {"max_spawn": kbd._positive_int(section.get("max_spawn"), None),
            "max_in_progress": kbd.resolve_max_in_progress(kbd._positive_int(section.get("max_in_progress"), None)),
            "max_in_progress_per_profile": kbd._positive_int(section.get("max_in_progress_per_profile"), None)}
    recorded = governors.snapshot(conn)
    if recorded.get("available") and not recorded.get("stale") and isinstance(recorded.get("caps"), dict):
        caps.update({key: recorded["caps"].get(key) for key in caps if key in recorded["caps"]})
    return Environment(
        board=governors._board(conn), profile_exists=kbd._profile_exists_fn(),
        default_assignee=kbd._resolve_default_assignee(section.get("default_assignee")),
        per_profile_cap=caps["max_in_progress_per_profile"], max_spawn=caps["max_spawn"],
        max_in_progress=caps["max_in_progress"], review_enabled=kbd.review_dispatch_enabled())


@contextlib.contextmanager
def read_only(conn: Any) -> Iterator[None]:
    """No predicate may write through this connection while the chain is walked."""
    prior = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only = ON")
    try:
        yield
    finally:
        conn.execute(f"PRAGMA query_only = {'ON' if prior else 'OFF'}")


def _scratch() -> Any:
    """A throwaway ``DispatchResult``: the hold functions record on it, nothing reads it afterwards."""
    from hermes_cli.kanban_db_dispatch import DispatchResult

    return DispatchResult()


@dataclass
class Card:
    conn: Any
    task_id: str
    row: Any
    task: Any
    lane: str
    assignee: Optional[str]
    env: Environment
    recorded: Optional[dict[str, Any]]


Answer = Optional[dict[str, Any]]  # None = pass; else {verdict?, scope?, reading?, threshold?, until?, detail?}


def _stop(**fields: Any) -> dict[str, Any]:
    return {"verdict": STOP, **{k: v for k, v in fields.items() if v is not None}}


def _recorded(card: Card, governor_id: str) -> Answer:
    """A tick-only rule: what the last recorded tick decided for this card."""
    rec = card.recorded
    if rec is None or rec.get("stale"):
        return {"verdict": UNKNOWN, "detail": {"reason": "asked only inside a dispatcher tick; no fresh tick recorded"}}
    named = [rec] + [o for o in (rec.get("overlaps") or []) if isinstance(o, dict)]
    hit = next((entry for entry in named if entry.get("governor") == governor_id), None)
    if hit is None:
        return {"verdict": PASS, "age_s": rec.get("age_s"), "detail": {"reason": "not charged by the last tick"}}
    return _stop(scope=hit.get("scope"), reading=hit.get("reading"), threshold=hit.get("threshold"),
                 detail=hit.get("detail"), age_s=rec.get("age_s"))


# --- card-level steps, in ``lane_rows`` -> ``_dispatch_once_locked`` -> ``_dispatch_lane_task`` -> claim order ------


def _containment(card: Card) -> Answer:
    from hermes_cli.kanban_containment_connections import held

    return _stop() if held(card.conn, card.task_id) else None


def _lane_paused(card: Card) -> Answer:
    from hermes_cli.kanban_lanes import drop_paused

    return None if drop_paused(card.conn, [card.row]) else _stop(**_lane(card, "lane_paused"))


def _lane_full(card: Card) -> Answer:
    from hermes_cli.kanban_lane_workers import drop_full

    return None if drop_full(card.conn, [card.row]) else _stop(**_lane(card, "lane_max_workers"))


def _lane(card: Card, governor_id: str) -> dict[str, Any]:
    from hermes_cli import kanban_lanes as lanes

    pol = lanes.safe_policy()
    if pol is None:
        return {}
    name = pol.lane_of(card.row["tenant"])
    return {"scope": name, **governors._lane_reading(card.conn, pol, name, governor_id, now=time.time())}


def _placement(card: Card) -> Answer:
    from hermes_cli.kanban_worker_node import dispatch_refusals

    if card.lane not in card.env.refusals:
        card.env.refusals[card.lane] = dispatch_refusals(card.conn, card.lane)
    reason = card.env.refusals[card.lane].get(card.task_id)
    return _stop(detail={"reason": reason}) if reason else None


def _unassigned(card: Card) -> Answer:
    if card.assignee:
        return None
    return _stop(detail={"reason": "no assignee" + ("" if card.lane == "ready" else " (review lane takes no default)")})


def _needs_assignee(step: Callable[[Card], Answer]) -> Callable[[Card], Answer]:
    def run(card: Card) -> Answer:
        if not card.assignee:
            return {"verdict": UNKNOWN, "detail": {"reason": "no assignee to judge"}}
        return step(card)
    return run


@_needs_assignee
def _nonspawnable(card: Card) -> Answer:
    exists = card.env.profile_exists
    if exists is None or exists(card.assignee):
        return None
    return _stop(scope=card.assignee, detail={"reason": "assignee is not a spawnable profile on this home"})


@_needs_assignee
def _capsule(card: Card) -> Answer:
    from hermes_cli.kanban_capsule_admission import dispatch_refusal

    code = dispatch_refusal(card.conn, card.task_id, card.assignee, None, dry_run=True)
    return _stop(detail={"reason": code}) if code is not None else None


def _refusal_fallback(card: Card) -> Answer:
    from hermes_cli.kanban_refusal_fallback import admission

    return _stop() if admission(card.conn, card.task_id, _scratch(), dry_run=True) else None


def _class_walls(card: Card) -> Answer:
    from hermes_cli.kanban_class_walls import unroutable_class

    why = unroutable_class(card.conn, card.task_id, lane=card.lane)
    return _stop(detail={"reason": why}) if why else None


def _review_wall(card: Card) -> Answer:
    from hermes_cli.kanban_review_admission import hold

    scratch = _scratch()
    if not hold(card.conn, card.task_id, scratch, lane=card.lane, dry_run=True):
        return None
    held = scratch.clock_held.get(card.task_id)
    return _stop(until=held[1] if held else None, detail={"reason": scratch.respawn_guarded[-1][1]})


def _foreign(card: Card) -> bool:
    from hermes_cli.kanban_wake_check import foreign_copy

    return foreign_copy(card.conn, card.task_id)


def _no_progress(card: Card) -> Answer:
    """``kanban_no_progress.hold``'s read: ``decide`` (the hold itself only writes after it says stop)."""
    from hermes_cli import kanban_no_progress as np

    if _foreign(card):
        return None  # a worker-node copy: its home board owns the decision
    budget = np.limit()
    runs = np.streak(card.conn, card.task_id)[0]
    verdict = np.decide(card.conn, card.task_id, budget=budget)
    answer = {"reading": runs, "threshold": budget or None}
    return {"verdict": PASS, **answer} if verdict is None else _stop(**answer, detail={"action": verdict[0]})


def _spend(card: Card) -> Answer:
    """``kanban_spend_budget.hold``'s reads: a pending decision, then ``over(spend, card_limits)``."""
    from hermes_cli import kanban_spend_budget as sb
    from hermes_cli import kanban_spend_decision as decision

    if _foreign(card):
        return None
    if decision.pending(card.conn, card.task_id):
        return _stop(detail={"reason": "spend decision pending"})
    lim = sb.card_limits(card.conn, card.task_id, sb.limits())
    if not lim.runs and not lim.cache_read_tokens:
        return None
    spent = sb.spend(card.conn, card.task_id)
    which = sb.over(spent, lim)
    answer = {"reading": {"runs": spent.runs, "cache_read_tokens": spent.tokens["cache_read_tokens"]},
              "threshold": {"runs": lim.runs, "cache_read_tokens": lim.cache_read_tokens}}
    return {"verdict": PASS, **answer} if which is None else _stop(**answer, detail={"limit": which})


def _provider_walls(card: Card) -> Answer:
    from hermes_cli.kanban_dispatch_skip import pinned_quota_hold

    held = pinned_quota_hold(card.conn, card.task_id) if card.lane == "ready" else None
    if held is None:
        return None
    return _stop(scope=str(held[1]), until=held[2], detail={"model": held[0], "provider": held[1]})


@_needs_assignee
def _profile_cap(card: Card) -> Answer:
    from hermes_cli import kanban_db as kb

    cap = card.env.per_profile_cap
    if cap is None:
        return None
    running = card.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status = 'running' AND assignee = ?" + kb._sync.held_elsewhere_sql(card.conn),
        (card.assignee,)).fetchone()[0]
    answer = {"scope": card.assignee, "reading": int(running), "threshold": cap}
    return _stop(**answer) if running >= cap else {"verdict": PASS, **answer}


def _model_pin(card: Card) -> Answer:
    from hermes_cli.kanban_model_assignment import route_model_mismatch

    code = route_model_mismatch(card.conn, card.task, dry_run=True)
    return _stop(detail={"reason": code}) if code else None


def _respawn_guard(card: Card) -> Answer:
    from hermes_cli.kanban_db_dispatch import check_respawn_guard

    reason = check_respawn_guard(card.conn, card.task_id, lane=card.lane)
    return _stop(detail={"reason": reason}) if reason else None


def _llm_pool(card: Card) -> Answer:
    from hermes_cli import kanban_llm_pacing_dispatch as pacing

    scratch = _scratch()
    reading = governors.pool_reading()
    if not pacing.hold(card.conn, card.row, card.lane, scratch, dry_run=True, refresh=False):
        return {"verdict": PASS, **reading}
    _tid, detail, until, extra = scratch.llm_paced[-1]
    pools = [p.get("provider") for p in (extra or {}).get("providers", ()) if isinstance(p, dict)]
    return _stop(until=until, reading=reading["reading"], threshold=reading["threshold"],
                 detail={"card_routes_at_reserve": pools, "held": detail, **reading["detail"]})


def _start_gate(card: Card) -> Answer:
    from hermes_cli import kanban_start_gate as gate

    reason = gate.hold(task_id=card.task_id, lane=card.lane, assignee=card.assignee, board=card.env.board)
    return _stop(scope=gate.word(reason), detail={"reason": gate.word(reason)}) if reason else None


@_needs_assignee
def _owner_down(card: Card) -> Answer:
    from hermes_cli.kanban_owner_down import probe

    down = probe(card.assignee)
    return _stop(scope=card.assignee, detail={"reason": down.code, "unit": down.unit}) if down is not None else None


def _quiet(card: Card) -> Answer:
    from agent.claim_quiet import read_active
    from hermes_cli.kanban_db_dispatch_admission import _native_local_destination

    lease = read_active()
    if lease is None or not _native_local_destination(card.conn, card.task_id, None):
        return None
    return _stop(until=lease.expires_at, detail={"episode": lease.episode_id})


def _update_fence(card: Card) -> Answer:
    from pathlib import Path

    from hermes_cli.admission_fence import SUPPORTED, is_held
    from hermes_constants import get_default_hermes_root

    return _stop() if SUPPORTED and is_held(Path(get_default_hermes_root()) / "core-update") else None


def _disk_floor(card: Card) -> Answer:
    from hermes_cli.kanban_disk_admission import refusal_reason

    reason = refusal_reason()
    return _stop(detail={"reason": reason}) if reason else None


def _worker_resources(card: Card) -> Answer:
    from hermes_cli.worker_compute_capacity import worker_enabled

    return _recorded(card, "worker_resources") if worker_enabled() else None


def _hub_cpu(card: Card) -> Answer:
    """The locked claim gate's two reads (``_final_hub_gate``): the root limit and the sampler's sample."""
    from hermes_cli import machine_cpu_metrics as cpu
    from hermes_cli.kanban_admission import HUB_CPU_LIMIT_UNKNOWN, sampler_sample

    state, limit = cpu.root_limit()
    if state == cpu.OFF:
        return None
    if state != cpu.ON:
        return _stop(detail={"reason": HUB_CPU_LIMIT_UNKNOWN})
    measured = sampler_sample()
    if measured is None:
        return {"verdict": UNKNOWN, "threshold": limit, "detail": {"reason": "no fresh CPU sample"}}
    pct = measured["cpu_busy_pct"]
    answer = {"reading": pct, "threshold": limit}
    return {"verdict": PASS, **answer} if cpu.below(pct, limit) else _stop(**answer)


def _tick_only(governor_id: str) -> Callable[[Card], Answer]:
    return lambda card: _recorded(card, governor_id)


CARD_STEPS: tuple[tuple[str, Callable[[Card], Answer]], ...] = (
    ("containment", _containment), ("lane_paused", _lane_paused), ("lane_max_workers", _lane_full),
    ("hub_claim_screen", _tick_only("hub_claim_screen")), ("placement", _placement),
    ("admission_pool", _tick_only("admission_pool")), ("unassigned", _unassigned),
    ("nonspawnable", _nonspawnable), ("capsule", _capsule), ("refusal_fallback", _refusal_fallback),
    ("class_walls", _class_walls), ("review_provider_wall", _review_wall), ("no_progress", _no_progress),
    ("spend_budget", _spend), ("provider_walls", _provider_walls), ("profile_cap", _profile_cap),
    ("model_pin", _model_pin), ("respawn_guard", _respawn_guard), ("llm_pool", _llm_pool),
    ("start_gate", _start_gate), ("owner_down", _owner_down), ("quiet", _quiet), ("update_fence", _update_fence),
    ("disk_floor", _disk_floor), ("worker_resources", _worker_resources), ("hub_cpu", _hub_cpu),
    ("board_lease", _tick_only("board_lease")),
)


def _entry(governor_id: str, answer: Answer, started: float, *, since: Any = None) -> dict[str, Any]:
    known = governors.governor(governor_id)
    answer = dict(answer or {})
    out = {"governor": governor_id, "label": known.label if known else governor_id,
           "kind": known.kind if known else governors.HOLD, "verdict": answer.pop("verdict", PASS),
           "scope": answer.pop("scope", None), "reading": answer.pop("reading", None),
           "threshold": answer.pop("threshold", None), "since": since,
           "source": known.source if known else None, "age_s": answer.pop("age_s", 0.0),
           "ms": round((time.monotonic() - started) * 1000, 1)}
    out.update(answer)  # until, detail
    return out


def _ask(governor_id: str, step: Callable[[], Answer]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        answer = step()
    except Exception as exc:  # health: allow BLE001 -- one rule's fault is that rule's ``unknown``, never a pass
        answer = {"verdict": UNKNOWN, "detail": {"error": f"{type(exc).__name__}: {exc}"[:300]}}
    return _entry(governor_id, answer, started)


def _load_card(conn: Any, task_id: str, env: Environment, recorded: Optional[dict]) -> Optional[Card]:
    from hermes_cli import kanban_db as kb

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    assignee = row["assignee"] or (env.default_assignee if row["status"] == "ready" else None)
    return Card(conn, task_id, row, kb.Task.from_row(row), row["status"], assignee, env, recorded)


def card_chain(conn: Any, task_id: str, env: Environment, *, after: Optional[str] = None,
               recorded: Optional[dict] = None) -> list[dict[str, Any]]:
    """The card-level rules for one ready/review card, in dispatcher order; ``after`` skips up to and including
    that governor (the tick asks only what follows a card's first stop). Read-only (:func:`read_only`)."""
    floor = governors.order_of(after) if after is not None else -1
    with read_only(conn):
        card = _load_card(conn, task_id, env, recorded)
        if card is None or card.lane not in LANES:
            return []
        return [_ask(name, lambda step=step: step(card)) for name, step in CARD_STEPS
                if governors.order_of(name) > floor]


# --- board-level steps: ``_dispatch_once_locked`` before any card is looked at -------------------------------------


def global_chain(conn: Any, env: Environment, recorded: Optional[dict] = None) -> list[dict[str, Any]]:
    """The rules that stop a whole tick, asked as the tick asks them."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_gateway_drain as drain

    out = [_ask("gateway_drain", lambda: _stop() if drain.dispatch_paused() else None),
           _ask("core_update_drain", lambda: _core_update(drain.core_update_hold()))]
    started = time.monotonic()
    scratch = _scratch()
    with governors.tick(conn, {}) as capture:  # the host census lands on a throwaway capture
        may_spawn, _budget = kbd._tick_spawn_budget(conn, scratch, max_spawn=env.max_spawn,
                                                    max_in_progress=env.max_in_progress, board=env.board)
    governors._take_capture(conn)
    stop = None if may_spawn else governors.global_stop(scratch, capture, board=env.board)
    own = capture.host.get("own", kbd.count_running_tasks(conn))
    others = capture.host.get("others")
    readings = {
        "max_spawn": {"reading": own, "threshold": env.max_spawn},
        "host_slots": {"reading": own + others if others is not None else None, "threshold": env.max_in_progress,
                       "detail": governors._host_detail(capture, env.board)},
        "memory_pressure": {"reading": scratch.memory_pressure or "ok", "threshold": "critical"},
    }
    for name in ("max_spawn", "host_slots"):
        hit = stop is not None and stop["governor"] == name
        out.append(_entry(name, {**readings[name], **(stop if hit else {}), "verdict": STOP if hit else PASS}, started))
    out.append(_entry("board_share", _recorded(SimpleNamespace(recorded=recorded), "board_share"), started))
    pressure = {"critical": STOP, "elevated": THROTTLE}.get(scratch.memory_pressure or "", PASS)
    out.append(_entry("memory_pressure", {**readings["memory_pressure"], "verdict": pressure}, started))
    return out


def _core_update(hold: Any) -> Answer:
    if hold is None:
        return None
    return _stop(detail={k: hold.get(k) for k in ("pid", "path") if isinstance(hold, dict) and hold.get(k)})


# --- cards that are not on a dispatch lane ----------------------------------------------------------------------


def _off_lane(conn: Any, row: Any) -> list[dict[str, Any]]:
    """Why a card that no tick looks at is waiting: open parents, a scheduled wake, or a lane gate."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_lanes as lanes

    started = time.monotonic()
    status = row["status"]
    if status == "todo":
        opened = [pid for pid, state in kb.parent_states(conn, row["id"]) if state not in ("done", "archived")]
        return [_entry("dependencies", _stop(reading=len(opened), threshold=0, detail={"open_parents": opened[:20]}),
                       started)]
    if status == "scheduled":
        return [_entry("schedule", _stop(), started)]
    if status == "triage" and conn.execute(f"SELECT 1 FROM tasks WHERE id = ? AND {lanes.HELD}", (row["id"],)).fetchone():
        name = next((gov for tid, _s, _t, gov in governors._gate_cards(conn) if tid == row["id"]), "lane_gate")
        pol = lanes.safe_policy()
        lane = pol.lane_of(row["tenant"]) if pol is not None else row["tenant"]
        reading = governors._lane_reading(conn, pol, lane, name, now=time.time()) if pol is not None else {}
        return [_entry(name, _stop(scope=lane, **reading), started)]
    return []


def explain(conn: Any, task_id: str, *, env: Optional[Environment] = None) -> dict[str, Any]:
    """Read-only dry run of the dispatcher's decision for one card (see the module docstring)."""
    started = time.monotonic()
    env = environment(conn) if env is None else env
    row = conn.execute("SELECT id, status, tenant, assignee, claim_lock FROM tasks WHERE id = ?", (task_id,)).fetchone()
    out: dict[str, Any] = {"task_id": task_id, "board": env.board, "source": "dry_run", "status": None,
                           "chain": [], "first_stop": None, "overlaps": [], "recorded": None}
    if row is None:
        return {**out, "error": "no such task", "elapsed_s": round(time.monotonic() - started, 3)}
    recorded = governors.card(conn, task_id)
    out.update(status=row["status"], recorded=recorded)
    if row["status"] in LANES and not row["claim_lock"] and (row["status"] == "ready" or env.review_enabled):
        chain = global_chain(conn, env, recorded) + card_chain(conn, task_id, env, recorded=recorded)
    else:
        with read_only(conn):
            chain = _off_lane(conn, row)
        if not chain:
            out["note"] = f"status {row['status']}: not waiting on the dispatcher"
    stops = [step for step in chain if step["verdict"] == STOP]
    if stops:
        first = stops[0]
        if recorded is not None and recorded.get("governor") == first["governor"]:
            first["since"] = recorded.get("since")
        else:
            first["since"] = _skip_since(conn, task_id)
    out.update(chain=chain, first_stop=stops[0] if stops else None, overlaps=stops[1:],
               elapsed_s=round(time.monotonic() - started, 3))
    return out


def _skip_since(conn: Any, task_id: str) -> Optional[int]:
    """When the ready episode's current recorded skip began (``kanban_dispatch_skip.latest``), if any."""
    try:
        from hermes_cli.kanban_dispatch_skip import latest

        skip = latest(conn, task_id)
        return int(skip["at"]) if skip and skip.get("at") else None
    except Exception:  # health: allow BLE001 -- ``since`` is evidence; the verdict stands without it
        return None
