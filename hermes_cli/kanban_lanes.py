"""Project lanes: who a card's work is for, and how worker slots are split between them. [daveey]

A card's lane is ``tasks.tenant``. ``kanban.admission.lanes`` in the installation-root config names the lanes::

    kanban:
      admission:
        lane_default: other          # lane of a card nothing else places
        agent_priority_cap: 100      # stored priority ceiling for cards the user did not create
        lanes:
          babybear: {share: 35, describe: "..."}
          hermes:
            share: 20
            gated: true              # cards filed by other cards or by detectors start held in triage
            admit_per_day: 50
            match: {creators: [self-heal], key_prefixes: ["ci-main-watch:"], repos: [hermes-agent], self_work: true}
            gate_exempt: ['independent review']   # title regexes that skip the gate under an admitted creator
            never_admit: ['lifecycle guard']      # title regexes the planner may never admit
          other: {share: 10}

Three things read it. Creation (:func:`initial_state`) assigns the lane (given, then a ``match`` rule, then the
parent's or creating card's lane, then the default) and holds a gated lane's agent-filed cards in ``triage`` with
``block_kind='lane_gate'``. Ordering (:func:`order`) gives each free slot to the lane running the fewest cards
relative to its share; a lane with nothing ready lends its share, so shares are targets and never ceilings.
:func:`usage` reports target against actual.

With no ``lanes`` mapping every function here is a no-op: creation, ordering and the triage sweep are unchanged.
A malformed mapping is logged and treated as absent on the creation and dispatch paths (a config typo must not stop
card creation); :func:`policy` raises it for the CLI.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

_LOG = logging.getLogger(__name__)

ASSIGNED, UNCLASSIFIED, GATED, ADMITTED = "lane_assigned", "lane_unclassified", "lane_gated", "lane_admitted"
GATE_KIND = "lane_gate"
USER = "user"
DEFAULT_LANE = "other"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_OPEN = "('triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review')"


@dataclass(frozen=True)
class Lane:
    name: str
    share: float = 0
    gated: bool = False
    admit_per_day: Optional[int] = None
    paused: bool = False
    describe: str = ""
    creators: frozenset = frozenset()
    key_prefixes: tuple = ()
    repos: frozenset = frozenset()
    self_work: bool = False
    gate_exempt: tuple = ()
    never_admit: tuple = ()


@dataclass(frozen=True)
class Policy:
    lanes: dict
    default: str
    priority_cap: Optional[int]

    def lane_of(self, tenant) -> str:
        return tenant if tenant in self.lanes else self.default


@dataclass(frozen=True)
class Decision:
    """What creation decided for one card; the empty decision is "lanes are not configured"."""
    lane: Optional[str] = None
    source: Optional[str] = None
    gated: bool = False
    cap: Optional[int] = None

    def priority(self, value: int) -> int:
        from hermes_cli.kanban_priority import bounded

        value = bounded(value)
        return value if self.cap is None else min(value, self.cap)


def _strings(raw, what) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v for v in raw):
        raise ValueError(f"lane {what} must be a list of non-empty strings")
    return tuple(raw)


def _patterns(raw, what) -> tuple:
    try:
        return tuple(re.compile(p, re.IGNORECASE) for p in _strings(raw, what))
    except re.error as exc:
        raise ValueError(f"lane {what}: {exc}") from exc


def _lane(name, raw) -> Lane:
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError(f"lane name {name!r} must match {_NAME.pattern}")
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError(f"lane {name} must be a mapping")
    known = {"share", "gated", "admit_per_day", "paused", "describe", "match", "gate_exempt", "never_admit"}
    if unknown := sorted(set(raw) - known):
        raise ValueError(f"lane {name}: unknown key(s) {', '.join(map(str, unknown))}")
    share = raw.get("share", 0)
    if type(share) not in (int, float) or share < 0:
        raise ValueError(f"lane {name}: share must be a nonnegative number")
    per_day = raw.get("admit_per_day")
    if per_day is not None and (type(per_day) is not int or per_day < 0):
        raise ValueError(f"lane {name}: admit_per_day must be a nonnegative integer")
    match = raw.get("match") or {}
    if not isinstance(match, dict) or set(match) - {"creators", "key_prefixes", "repos", "self_work"}:
        raise ValueError(f"lane {name}: match takes creators, key_prefixes, repos, self_work")
    return Lane(
        name=name, share=float(share), gated=raw.get("gated") is True, admit_per_day=per_day,
        paused=raw.get("paused") is True, describe=str(raw.get("describe") or ""),
        creators=frozenset(_strings(match.get("creators"), "match.creators")),
        key_prefixes=_strings(match.get("key_prefixes"), "match.key_prefixes"),
        repos=frozenset(_strings(match.get("repos"), "match.repos")), self_work=match.get("self_work") is True,
        gate_exempt=_patterns(raw.get("gate_exempt"), "gate_exempt"),
        never_admit=_patterns(raw.get("never_admit"), "never_admit"))


def parse(admission: dict) -> Optional[Policy]:
    """The lane policy in a ``kanban.admission`` mapping; None when it names no lanes. Raises ValueError."""
    raw = admission.get("lanes")
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("kanban.admission.lanes must be a mapping of lane name to settings")
    lanes = {name: _lane(name, spec) for name, spec in raw.items()}
    default = admission.get("lane_default", DEFAULT_LANE)
    if default not in lanes:
        raise ValueError(f"kanban.admission.lane_default {default!r} is not a configured lane")
    cap = admission.get("agent_priority_cap")
    if cap is not None and type(cap) is not int:
        raise ValueError("kanban.admission.agent_priority_cap must be an integer")
    return Policy(lanes, default, cap)


def policy() -> Optional[Policy]:
    from hermes_cli.kanban_admission_pools import admission_policy

    return parse(admission_policy())


def safe_policy() -> Optional[Policy]:
    """:func:`policy` for creation and dispatch: a malformed mapping is logged and reads as no lanes."""
    try:
        return policy()
    except ValueError:
        _LOG.warning("kanban.admission.lanes is malformed; lanes are off until it is fixed", exc_info=True)
        return None


# ── creation ────────────────────────────────────────────────────────────────

def _key_matches(lane: Lane, key) -> bool:
    return isinstance(key, str) and bool(lane.key_prefixes) and key.startswith(lane.key_prefixes)


def _rule_lane(pol: Policy, opts: dict) -> Optional[str]:
    repos = {os.path.basename(str(p).rstrip("/")) for p in (opts.get("project_repo"), opts.get("workspace_path"))
             if p}
    for lane in pol.lanes.values():
        if (opts.get("created_by") in lane.creators or _key_matches(lane, opts.get("idempotency_key"))
                or (lane.self_work and opts.get("self_work") is not None) or repos & lane.repos):
            return lane.name
    return None


def _creator_row(conn, opts):
    creator = opts.get("creator_task_id")
    if not creator:
        return None
    return conn.execute("SELECT tenant, status, block_kind FROM tasks WHERE id = ?", (creator,)).fetchone()


def _classify(conn, pol: Policy, given, inherited, opts) -> tuple[str, str]:
    if given:
        return given, "given"
    if lane := _rule_lane(pol, opts):
        return lane, "rule"
    if not inherited and (row := _creator_row(conn, opts)) is not None:
        inherited = row["tenant"]
    if inherited:
        return inherited, "inherited"
    return pol.default, "unclassified"


def _gated(conn, pol: Policy, lane_name: str, opts: dict) -> bool:
    """An agent-filed card in a gated lane waits for admission; the user's own cards and alarms do not."""
    from hermes_cli.kanban_admission_score import _alarm_key

    lane = pol.lanes.get(lane_name)
    if lane is None or not lane.gated or opts.get("created_by") == USER:
        return False
    key = opts.get("idempotency_key")
    filed_by_agent = bool(opts.get("creator_task_id")) or opts.get("created_by") in lane.creators \
        or _key_matches(lane, key)
    if not filed_by_agent or _alarm_key(key) or opts.get("admission_class") == "urgent":
        return False
    title = opts.get("title") or ""
    if any(p.search(title) for p in lane.gate_exempt):
        # Review and land work of an admitted card must be able to finish it.
        row = _creator_row(conn, opts)
        if row is not None and not (row["status"] == "triage" and row["block_kind"] == GATE_KIND):
            return False
    return True


def initial_state(conn, parents, initial_status, triage, tenant, opts) -> tuple[str, Optional[str], Decision]:
    """``kanban_db_graph.initial_task_state`` plus the lane decision, under the creator's write transaction.

    ``opts`` is ``create_task``'s locals. Only a card that would start ``ready`` or ``todo`` is gated: an explicit
    triage, blocked or running start is the creator's own parking and keeps its meaning.
    """
    from hermes_cli.kanban_db_graph import initial_task_state

    status, inherited = initial_task_state(conn, parents, initial_status, triage, tenant)
    pol = safe_policy()
    if pol is None:
        return status, inherited, Decision()
    lane, source = _classify(conn, pol, tenant, inherited, opts)
    gated = status in ("ready", "todo") and _gated(conn, pol, lane, opts)
    cap = None if opts.get("created_by") == USER else pol.priority_cap
    return ("triage" if gated else status), lane, Decision(lane, source, gated, cap)


def record(conn, task_id: str, decision: Optional[Decision]) -> None:
    """Persist a creation decision's events (and the gate mark) in the creator's transaction."""
    if decision is None or decision.lane is None:
        return
    from hermes_cli.kanban_db import _append_event

    _append_event(conn, task_id, ASSIGNED, {"lane": decision.lane, "source": decision.source})
    if decision.source == "unclassified":
        _append_event(conn, task_id, UNCLASSIFIED, {"lane": decision.lane})
    if decision.gated:
        conn.execute("UPDATE tasks SET block_kind = ? WHERE id = ?", (GATE_KIND, task_id))
        _append_event(conn, task_id, GATED, {"lane": decision.lane})


# ── moves after creation ────────────────────────────────────────────────────

def assign(conn, task_id: str, lane: str, *, source: str, reason: str, pol: Optional[Policy] = None) -> None:
    """Move a card to ``lane``; raises ValueError for an unknown lane or card."""
    from hermes_cli import kanban_db as kb

    pol = policy() if pol is None else pol
    if pol is None or lane not in pol.lanes:
        raise ValueError(f"unknown lane: {lane}")
    with kb.write_txn(conn):
        row = conn.execute("SELECT tenant FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"no such task: {task_id}")
        conn.execute("UPDATE tasks SET tenant = ? WHERE id = ?", (lane, task_id))
        kb._append_event(conn, task_id, ASSIGNED, {"lane": lane, "from": row["tenant"], "source": source,
                                                   "reason": reason})


def held(conn, lane: Optional[str] = None) -> list[sqlite3.Row]:
    """Cards waiting at a lane gate, oldest first."""
    sql = ("SELECT id, title, tenant, priority, created_at, created_by FROM tasks "
           "WHERE status = 'triage' AND block_kind = ?")
    params: tuple = (GATE_KIND,)
    if lane is not None:
        sql, params = sql + " AND tenant = ?", (GATE_KIND, lane)
    return conn.execute(sql + " ORDER BY created_at, id", params).fetchall()


def day_start(now: Optional[float] = None) -> int:
    """Start of the host's local day: the admit budget is per local day."""
    local = time.localtime(time.time() if now is None else now)
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)))


def admitted_today(conn, lane: str, *, now: Optional[float] = None) -> int:
    """Budgeted admissions into ``lane`` since the local day began."""
    return conn.execute(
        "SELECT COUNT(*) FROM task_events e WHERE e.kind = ? AND e.created_at >= ? "
        "AND json_extract(e.payload, '$.lane') = ? AND COALESCE(json_extract(e.payload, '$.budget'), 1) = 1",
        (ADMITTED, day_start(now), lane)).fetchone()[0]


def admit(conn, task_id: str, *, actor: str, reason: str, budget: bool = True) -> bool:
    """Release a gate-held card to ``ready`` (``todo`` while a parent is open). False when it is not held."""
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute("SELECT tenant FROM tasks WHERE id = ? AND status = 'triage' AND block_kind = ?",
                           (task_id, GATE_KIND)).fetchone()
        if row is None:
            return False
        waiting = any(state not in ("done", "archived") for _pid, state in kb.parent_states(conn, task_id))
        conn.execute("UPDATE tasks SET status = ?, block_kind = NULL WHERE id = ?",
                     ("todo" if waiting else "ready", task_id))
        kb._append_event(conn, task_id, ADMITTED, {"lane": row["tenant"], "actor": actor, "reason": reason,
                                                   "budget": 1 if budget else 0})
    return True


def release_blocker(conn, parent_id: str, child_id: str) -> None:
    """A gate-held card that another lane's open card now waits on is admitted outside the budget."""
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, tenant, status, block_kind, status IN {_OPEN} AS open FROM tasks WHERE id IN (?, ?)",
        (parent_id, child_id))}
    parent, child = rows.get(parent_id), rows.get(child_id)
    if parent is None or child is None or not child["open"]:
        return
    if parent["status"] != "triage" or parent["block_kind"] != GATE_KIND:
        return
    pol = safe_policy()
    if pol is None or pol.lane_of(child["tenant"]) == pol.lane_of(parent["tenant"]):
        return
    admit(conn, parent_id, actor="link", reason=f"{child_id} in lane {pol.lane_of(child['tenant'])} waits on it",
          budget=False)


# ── ordering ────────────────────────────────────────────────────────────────

def _running(conn, pol: Policy) -> dict[str, int]:
    """Running cards per lane across the fleet: the full replica, with local rows overriding it."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn

    lanes: dict[str, str] = {}
    if sync.board_mode(conn) == "primary":
        replica = None
        try:
            replica = wn.open_replica(conn)
            if replica is not None:
                lanes = {r[0]: r[1] for r in replica.execute("SELECT id, tenant FROM tasks WHERE status = 'running'")}
        except (sqlite3.Error, OSError, RuntimeError):
            lanes = {}
        finally:
            if replica is not None:
                replica.close()
    if lanes:
        marks = ",".join("?" for _ in lanes)
        for tid, status in conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({marks})", tuple(lanes)):
            if status != "running":
                del lanes[tid]
    lanes.update((r[0], r[1]) for r in conn.execute("SELECT id, tenant FROM tasks WHERE status = 'running'"))
    counts = dict.fromkeys(pol.lanes, 0)
    for tenant in lanes.values():
        counts[pol.lane_of(tenant)] += 1
    return counts


def _candidate_lanes(conn, pol: Policy, ids: list[str]) -> tuple[dict[str, str], set[str]]:
    from hermes_cli.kanban_admission_score import _alarm_key

    lanes, alarms = {}, set()
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ",".join("?" for _ in batch)
        for tid, tenant, key in conn.execute(
                f"SELECT id, tenant, idempotency_key FROM tasks WHERE id IN ({marks})", batch):
            lanes[tid] = pol.lane_of(tenant)
            if _alarm_key(key):
                alarms.add(tid)
    return lanes, alarms


def order(conn, scored: list, *, pol: Optional[Policy] = None) -> list:
    """Reorder ``kanban_admission_score.rank``'s sorted ``(row, breakdown)`` list by lane share.

    Land-turn owners, marked classes and alarm cards keep their place at the front. Every other pick goes to
    the lane with the fewest ``(running + already picked) / share``; inside a lane the score order is kept.
    Cards of a paused lane sort last (``drop_paused`` removes them from dispatch). No lanes = unchanged.
    """
    pol = safe_policy() if pol is None else pol
    if pol is None or len(scored) < 2:
        return scored
    lanes, alarms = _candidate_lanes(conn, pol, [row["id"] for row, _parts in scored])
    front, queues, paused = [], {}, []
    for index, item in enumerate(scored):
        row, parts = item
        lane = pol.lanes[lanes.get(row["id"], pol.default)]
        if parts.turn_owner or parts.class_rank > 0 or row["id"] in alarms:
            front.append(item)
        elif lane.paused:
            paused.append(item)
        else:
            queues.setdefault(lane.name, []).append((index, item))
    if len(queues) < 2:
        return front + [item for queue in queues.values() for _i, item in queue] + paused
    load = _running(conn, pol)
    out = []
    while queues:
        def cost(name):
            share = pol.lanes[name].share
            return (load[name] / share if share > 0 else float("inf"), queues[name][0][0])
        name = min(queues, key=cost)
        out.append(queues[name].pop(0)[1])
        load[name] += 1
        if not queues[name]:
            del queues[name]
    return front + out + paused


def drop_paused(conn, rows: list) -> list:
    """Dispatch candidates minus cards of a paused lane (alarm and marked cards are never paused)."""
    pol = safe_policy()
    if pol is None or not rows or not any(lane.paused for lane in pol.lanes.values()):
        return rows
    lanes, alarms = _candidate_lanes(conn, pol, [row["id"] for row in rows])

    def keep(row) -> bool:
        try:
            marked = row["admission_class"] in ("capacity", "urgent")
        except (IndexError, KeyError):
            marked = False
        return marked or row["id"] in alarms or not pol.lanes[lanes.get(row["id"], pol.default)].paused
    return [row for row in rows if keep(row)]


# ── report ──────────────────────────────────────────────────────────────────

def usage(conn, pol: Policy, *, window_s: int, now: Optional[float] = None) -> dict[str, dict]:
    """Per lane on one connection: target and actual share of run-seconds in the window, and what is waiting."""
    now = int(time.time() if now is None else now)
    cutoff = now - window_s
    out = {name: {"share": lane.share, "paused": lane.paused, "gated": lane.gated,
                  "admit_per_day": lane.admit_per_day, "run_seconds": 0, "runs": 0, "running": 0, "ready": 0,
                  "held": 0, "open": 0, "oldest_ready_s": None, "admitted_today": 0}
           for name, lane in pol.lanes.items()}
    for tenant, seconds, runs in conn.execute(
            "SELECT t.tenant, SUM(MIN(COALESCE(r.ended_at, ?), ?) - MAX(r.started_at, ?)), COUNT(*) "
            "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.started_at IS NOT NULL AND r.started_at < ? AND COALESCE(r.ended_at, ?) > ? GROUP BY t.tenant",
            (now, now, cutoff, now, now, cutoff)):
        row = out[pol.lane_of(tenant)]
        row["run_seconds"] += int(seconds or 0)
        row["runs"] += runs
    for tenant, status, kind, count, oldest in conn.execute(
            f"SELECT tenant, status, COALESCE(block_kind, ''), COUNT(*), MIN(created_at) "
            f"FROM tasks WHERE status IN {_OPEN} GROUP BY 1, 2, 3"):
        row = out[pol.lane_of(tenant)]
        row["open"] += count
        if status == "running":
            row["running"] += count
        elif status == "ready":
            row["ready"] += count
            age = now - int(oldest or now)
            row["oldest_ready_s"] = age if row["oldest_ready_s"] is None else max(row["oldest_ready_s"], age)
        elif status == "triage" and kind == GATE_KIND:
            row["held"] += count
    total_share = sum(lane.share for lane in pol.lanes.values() if not lane.paused) or 1
    total_run = sum(row["run_seconds"] for row in out.values()) or 1
    for name, row in out.items():
        row["target"] = 0.0 if row["paused"] else round(row["share"] / total_share, 4)
        row["actual"] = round(row["run_seconds"] / total_run, 4)
        row["admitted_today"] = admitted_today(conn, name, now=now) if row["gated"] else 0
    return out
