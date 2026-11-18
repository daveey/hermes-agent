"""Project lanes: who a card's work is for, and how worker slots are split between them. [daveey]

A card's lane is ``tasks.tenant``. ``kanban.admission.lanes`` in the installation-root config names the lanes::

    kanban:
      admission:
        lane_default: other          # lane of a card nothing else places
        lane_shadow: false           # true = classify and report only: no gate, no cap, no reordering
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

``lane_shadow: true`` is the rollout step before enforcement: cards are classified and reported, and each
``lane_assigned`` event says whether the card would have been held (``would_gate``), but nothing is held, capped,
reordered or paused.

With no ``lanes`` mapping every function here is a no-op: creation, ordering and the triage sweep are unchanged.
A malformed mapping is logged and treated as absent on the creation and dispatch paths (a config typo must not stop
card creation); :func:`policy` raises it for the CLI.
"""
from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

_LOG = logging.getLogger(__name__)

ASSIGNED, UNCLASSIFIED, GATED, ADMITTED = "lane_assigned", "lane_unclassified", "lane_gated", "lane_admitted"
GATE_KIND = "lane_gate"
# Creator labels a person's own surfaces write: the CLI in a human shell and the dashboard. A soft policy
# input, not authorization: an agent shell that writes "user" is caught by ``_by_user`` below.
USER_CREATORS = frozenset({"user", "dashboard"})
# Kernel recovery cards (resume requester, no-progress stop) exist to get a stuck card moving; holding them
# strands the card they were filed for.
RECOVERY_CREATORS = frozenset({"kanban-recovery", "kanban-no-progress"})
DEFAULT_LANE = "other"
# A card waiting at a lane gate: in triage with the gate mark, never started and never released. ``promote`` and
# ``unblock`` leave ``block_kind`` in place, so the mark alone would make a card that was released by one of
# them (or admitted, ran, and was later sent back to triage by a spend or no-progress stop) read as held again.
def held_sql(alias: str = "tasks") -> str:
    # COALESCE: a NULL block_kind must make this false, not NULL, or ``NOT (held)`` drops every unmarked card.
    return (f"{alias}.status = 'triage' AND COALESCE({alias}.block_kind, '') = 'lane_gate' "
            f"AND NOT EXISTS (SELECT 1 FROM task_runs hr WHERE hr.task_id = {alias}.id) "
            f"AND NOT EXISTS (SELECT 1 FROM task_events he WHERE he.task_id = {alias}.id "
            f"AND he.kind IN ('lane_admitted', 'promoted_manual', 'unblocked', 'triage_swept', 'decomposed', 'specified'))")


HELD = held_sql()
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
    shadow: bool = False

    def lane_of(self, tenant) -> str:
        return tenant if tenant in self.lanes else self.default


@dataclass(frozen=True)
class Decision:
    """What creation decided for one card; the empty decision is "lanes are not configured"."""
    lane: Optional[str] = None
    source: Optional[str] = None
    gated: bool = False
    cap: Optional[int] = None
    would_gate: bool = False

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
    if type(share) not in (int, float) or not math.isfinite(share) or share < 0:
        raise ValueError(f"lane {name}: share must be a finite nonnegative number")
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
    shadow = admission.get("lane_shadow", False)
    if type(shadow) is not bool:
        raise ValueError("kanban.admission.lane_shadow must be true or false")
    return Policy(lanes, default, cap, shadow)


def policy() -> Optional[Policy]:
    from hermes_cli.kanban_admission_pools import admission_policy

    return parse(admission_policy())


_WARNED: set = set()
_LAST_GOOD: list = []


def safe_policy() -> Optional[Policy]:
    """:func:`policy` for creation and dispatch; never raises.

    A malformed mapping reads as no lanes (the config says something, and it is wrong). A config that cannot
    be read at all (backend outage, I/O error) keeps the last policy this process read, so an outage does not
    open the gate; with none read yet it is no lanes. Logged once per distinct error per process.
    """
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        pol = policy()
    except ValueError as exc:
        _warn(exc)
        _LAST_GOOD.clear()  # the config was read and is wrong: an older policy must not come back later
        return None
    # ConfigBackendUnavailable is a SystemExit, so ``Exception`` alone would let a config outage stop creation.
    except (Exception, ConfigBackendUnavailable) as exc:  # health: allow BLE001 -- config must never stop creation
        _warn(exc)
        kept = list(_LAST_GOOD)  # one read: another thread may clear the list between a check and an index
        return kept[0] if kept else None
    _LAST_GOOD[:] = [pol]
    return pol


def _warn(exc) -> None:
    key = f"{type(exc).__name__}: {exc}"
    if key not in _WARNED:
        _WARNED.add(key)
        _LOG.warning("kanban.admission.lanes is unreadable or malformed: %s", key)


def enforced() -> Optional[Policy]:
    """The policy when lanes are configured and not in shadow mode, else None."""
    pol = safe_policy()
    return None if pol is None or pol.shadow else pol


def held_exclusion(alias: str = "tasks") -> str:
    """SQL (on table alias ``alias``) that keeps gate-held cards out of a triage consumer while the gate is enforced;
    empty when lanes are off or in shadow, so held cards left behind by a rollback are swept like any other."""
    return f" AND NOT ({held_sql(alias)})" if enforced() is not None else ""


def gate_refusal(conn, task_id: str) -> Optional[str]:
    """Why a card must not be specified, decomposed or swept: it waits at an enforced lane gate."""
    if enforced() is None:
        return None
    row = conn.execute(f"SELECT 1 FROM tasks WHERE id = ? AND {HELD}", (task_id,)).fetchone()
    if row is None:
        return None
    return f"task waits at its lane gate; release it with `hermes kanban lanes admit {task_id}`"


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


def _agent_task(opts: dict) -> Optional[str]:
    """The card whose worker is creating this one: the explicit creator, else the card this process owns.

    A delegated child or a cron run beside a worker inherits ``HERMES_KANBAN_TASK`` without owning the card, so
    the environment counts only in the dispatcher-owned worker context.
    """
    if creator := opts.get("creator_task_id"):
        return creator
    owned = os.environ.get("HERMES_KANBAN_WORKER_TASK") or os.environ.get("HERMES_KANBAN_TASK")
    if not owned:
        return None
    from agent.delegation_context import is_dispatcher_owned_worker_context

    return owned if is_dispatcher_owned_worker_context() else None


def _human_shell() -> bool:
    """Whether this process is a person's shell, not an agent's terminal (chat, cron or worker)."""
    from hermes_cli.kanban_cli_actor import cli_actor

    return cli_actor() == "user"


def cli_creator(args, label: str) -> str:
    """``created_by`` for a CLI create. ``--created-by`` defaults to ``user`` for every caller and takes any
    string, so the user labels are kept only for a person: a typed slash command or a human shell. Anything
    else asking for one is stored as ``agent``. Only while lanes are enforced: with no lanes, or in shadow mode,
    the label is stored as given, because shadow changes nothing an agent or a reader can observe on a card
    beyond its lane. (Shadow's ``would_gate`` therefore reads a chat or cron agent's terminal create as the
    person's; worker shells and tool creates are still recognised by their card.)"""
    if label not in USER_CREATORS or enforced() is None:
        return label
    return label if cli_person(args) else "agent"


def cli_person(args) -> bool:
    """Whether a CLI call is the person's: a slash command they typed (which runs inside a process that
    carries agent session markers) or a human shell. Known limit: an agent that strips its environment, for
    example over ssh, looks like a human shell."""
    return bool(getattr(args, "_slash_typed", False)) or _human_shell()


def _by_user(opts: dict) -> bool:
    """Created by the person: a user label (the CLI keeps those for people only, see :func:`cli_creator`; the
    dashboard writes its own) and no worker card behind the call."""
    return opts.get("created_by") in USER_CREATORS and _agent_task(opts) is None


def _creator_row(conn, opts):
    creator = _agent_task(opts)
    if not creator:
        return None
    return conn.execute("SELECT tenant, status, block_kind FROM tasks WHERE id = ?", (creator,)).fetchone()


def _classify(conn, pol: Policy, given, inherited, opts) -> tuple[str, str]:
    """``given`` is the user's explicit lane. An agent's is only a hint: workers inherit ``HERMES_TENANT`` from
    their card and pass it on every create, so a ``match`` rule must win over it."""
    if given and _by_user(opts):
        return given, "given"
    if lane := _rule_lane(pol, opts):
        return lane, "rule"
    if given:
        return given, "inherited"
    if not inherited and (row := _creator_row(conn, opts)) is not None:
        inherited = row["tenant"]
    if inherited:
        return inherited, "inherited"
    return pol.default, "unclassified"


def _gated(conn, pol: Policy, lane_name: str, opts: dict) -> bool:
    """An agent-filed card in a gated lane waits for admission; the user's own cards and alarms do not."""
    from hermes_cli.kanban_admission_score import _alarm_key

    lane = pol.lanes[pol.lane_of(lane_name)]  # a tenant that names no lane is the default lane, gate included
    if not lane.gated or _by_user(opts) or opts.get("created_by") in RECOVERY_CREATORS:
        return False
    key = opts.get("idempotency_key")
    filed_by_agent = bool(_agent_task(opts)) or opts.get("created_by") in lane.creators \
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


def lane_tenant(conn, parents, initial_status, triage, tenant, opts) -> Optional[str]:
    """The tenant ``create_task(**opts)`` will store, for callers that must match existing cards before creating.

    ``create_task`` classifies on its resolved project repository, so resolve it the same way here.
    """
    from hermes_cli import kanban_db as kb

    opts = dict(opts)
    if "project_repo" not in opts:
        kind, project = opts.get("workspace_kind"), opts.get("project_id")
        board = {}
        if (project is None and kind != "scratch") or opts.get("workspace_path") is None:
            try:
                board = kb._board_meta_for(opts.get("board"))
            except Exception:  # health: allow BLE001 -- create_task ignores an unreadable board the same way
                board = {}
        if project is None and kind != "scratch":
            project = (board.get("project_id") or "").strip() or None
        project, _obj, repo, kind = kb._resolve_project_link(
            conn, project, opts.get("project_source_task_id"), kind or "scratch", opts.get("workspace_path"))
        opts["project_repo"] = repo
        if opts.get("workspace_path") is None and repo is None and kind in {"dir", "worktree"}:
            opts["workspace_path"] = board.get("default_workdir")
    return initial_state(conn, parents, initial_status, triage, tenant, opts)[1]


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
    if pol.shadow:
        return status, lane, Decision(lane, source, would_gate=gated)
    cap = None if _by_user(opts) else pol.priority_cap
    return ("triage" if gated else status), lane, Decision(lane, source, gated, cap, gated)


def record(conn, task_id: str, decision: Optional[Decision]) -> None:
    """Persist a creation decision's events (and the gate mark) in the creator's transaction."""
    if decision is None or decision.lane is None:
        return
    from hermes_cli.kanban_db import _append_event

    _append_event(conn, task_id, ASSIGNED, {"lane": decision.lane, "source": decision.source,
                                            "would_gate": decision.would_gate})
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
    """Cards waiting at a lane gate, oldest first; ``lane`` filters by the lane a card counts in."""
    rows = conn.execute(f"SELECT id, title, tenant, priority, created_at, created_by FROM tasks WHERE {HELD} "
                        "ORDER BY created_at, id").fetchall()
    if lane is None:
        return rows
    pol = safe_policy()
    return [row for row in rows if (row["tenant"] if pol is None else pol.lane_of(row["tenant"])) == lane]


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
        row = conn.execute(f"SELECT tenant FROM tasks WHERE id = ? AND {HELD}", (task_id,)).fetchone()
        if row is None:
            return False
        waiting = any(state not in ("done", "archived") for _pid, state in kb.parent_states(conn, task_id))
        # Pinned to triage: an admit only ever moves a parked card, never a running one.
        conn.execute("UPDATE tasks SET status = ?, block_kind = NULL WHERE id = ? AND status = 'triage'",
                     ("todo" if waiting else "ready", task_id))
        pol = safe_policy()
        lane = row["tenant"] if pol is None else pol.lane_of(row["tenant"])  # the lane whose budget this spends
        kb._append_event(conn, task_id, ADMITTED, {"lane": lane, "actor": actor, "reason": reason,
                                                   "budget": 1 if budget else 0})
    return True


def release_blocker(conn, parent_id: str, child_id: str) -> None:
    """A gate-held card that another lane's runnable card now waits on is admitted outside the budget.

    Not for a parked or held waiter, and not for a title the lane never auto-admits.
    """
    parent = conn.execute(f"SELECT title, tenant FROM tasks WHERE id = ? AND {HELD}", (parent_id,)).fetchone()
    if parent is None:
        return
    # Runnable: a waiter parked in triage, blocked or scheduled is not waiting to run. This also covers a
    # waiter that is itself being created held (its gate mark is written after its parent links).
    child = conn.execute("SELECT tenant FROM tasks WHERE id = ? AND status IN ('todo', 'ready', 'running', 'review')",
                         (child_id,)).fetchone()
    if child is None:
        return
    pol = enforced()
    if pol is None or pol.lane_of(child["tenant"]) == pol.lane_of(parent["tenant"]):
        return
    lane = pol.lanes[pol.lane_of(parent["tenant"])]
    if any(p.search(parent["title"] or "") for p in lane.never_admit):
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

    Land-turn owners, marked classes and alarm cards go first, in their score order, ahead of every lane pick. Every other pick goes to
    the lane with the fewest ``(running + already picked) / share``; inside a lane the score order is kept.
    Cards of a paused lane sort last (``drop_paused`` removes them from dispatch). No lanes = unchanged.
    """
    pol = safe_policy() if pol is None else pol
    if pol is None or pol.shadow or len(scored) < 2:
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
    if pol is None or pol.shadow or not rows or not any(lane.paused for lane in pol.lanes.values()):
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
                  "held": 0, "open": 0, "oldest_ready_s": None, "admitted_today": 0, "done": 0, "cost_usd": 0.0,
                  "tokens": 0, "usage_runs": 0}
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
    for tenant, count in conn.execute(
            "SELECT tenant, COUNT(*) FROM tasks WHERE status = 'done' AND completed_at > ? GROUP BY tenant", (cutoff,)):
        out[pol.lane_of(tenant)]["done"] += count
    # Cost is what managed workers reported on their runs' receipts (kanban_spend_budget.usage_fields): an
    # estimate, and only for runs that reported; ``usage_runs`` against ``runs`` says how much that covers.
    for tenant, cost, tokens, runs in conn.execute(
            "SELECT tenant, SUM(cost), SUM(tokens), COUNT(*) FROM ("
            "SELECT t.tenant AS tenant, MAX(COALESCE(json_extract(e.payload, '$.usage.cost_usd'), 0)) AS cost, "
            "MAX(COALESCE(json_extract(e.payload, '$.usage.input_tokens'), 0) "
            "+ COALESCE(json_extract(e.payload, '$.usage.output_tokens'), 0)) AS tokens "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id WHERE e.kind = 'worker_result' "
            "AND e.created_at > ? AND json_extract(e.payload, '$.usage') IS NOT NULL "
            "GROUP BY e.task_id, e.run_id) GROUP BY tenant", (cutoff,)):
        row = out[pol.lane_of(tenant)]
        row["cost_usd"] = round(row["cost_usd"] + float(cost or 0), 2)
        row["tokens"] += int(tokens or 0)
        row["usage_runs"] += runs
    for tenant, count in conn.execute(f"SELECT tenant, COUNT(*) FROM tasks WHERE {HELD} GROUP BY tenant"):
        out[pol.lane_of(tenant)]["held"] += count
    total_share = sum(lane.share for lane in pol.lanes.values() if not lane.paused) or 1
    total_run = sum(row["run_seconds"] for row in out.values()) or 1
    for name, row in out.items():
        row["target"] = 0.0 if row["paused"] else round(row["share"] / total_share, 4)
        row["actual"] = round(row["run_seconds"] / total_run, 4)
        row["admitted_today"] = admitted_today(conn, name, now=now) if row["gated"] else 0
    return out
