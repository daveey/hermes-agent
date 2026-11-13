"""Kanban decomposer — fan a triage task out into a graph of child tasks.

Invoked by ``hermes kanban decompose [task_id | --all]`` and the gateway
dispatcher's auto-decompose path. Reads the profile roster (with
descriptions), asks the auxiliary LLM for a task graph in JSON, then
atomically creates the children, links them under the root, and flips the
root ``triage -> todo``. The root stays alive as parent of every leaf child so
it wakes back up when the graph completes and its assignee (the orchestrator
profile) can judge completion and add more work.

Mirrors ``kanban_specify`` (lazy aux import, lenient parse, never raises on
expected failures). ``fanout=false`` collapses to the ``specify`` behaviour
(tighten + promote, no children), making ``decompose`` a strict superset.
Unknown assignees are rewritten to ``default_assignee`` — a child NEVER ends
up with ``assignee=None``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_graph import decompose_triage_task
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import profiles as profiles_mod
from hermes_cli.kanban_specify import (
    _call_aux, _extract_json_blob, _load_triage_task, _task_prompt_fields, _title_body,
)
from hermes_cli.kanban_specify import _profile_author as _specify_author

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """You are the Kanban decomposer for the Hermes Agent board.

A user dropped a rough idea into the Triage column. Your job is to break it
into a small graph of concrete child tasks and route each one to the best-
matching profile from the available roster.

You will be given:
  - The original task title and body
  - The list of available profiles (each with name + description)
  - The fallback "default_assignee" used when no profile fits

Output a single JSON object with this exact shape:

  {
    "fanout": true,
    "rationale": "<one sentence on why this decomposition>",
    "tasks": [
      {
        "title": "<concrete task title, imperative voice, <= 80 chars>",
        "body":  "<detailed spec for the worker on this child task>",
        "assignee": "<profile name from the roster, or null for default>",
        "requires": ["<terminal, file, git, or host_repo when needed>"],
        "parents": [<int>, ...]
      },
      ...
    ]
  }

Rules:
  - "parents" is a list of INDICES (0-based) into this same "tasks" list,
    expressing actual data dependencies. Tasks with no parents run in
    PARALLEL. Tasks with parents wait until every parent completes.
  - Prefer parallelism. If two tasks can be done independently, give
    them no parents so the dispatcher fans them out at once.
  - Use 2-6 tasks for normal work. Don't create 20 tiny tasks. Don't
    cram everything into 1 task.
  - Pick assignees from the roster by matching the task to the profile's
    DESCRIPTION (not just the name). When nothing matches well, use null
    and the system will route to the default_assignee.
  - Declare explicit "requires" for every child: terminal for shell work, file for
    filesystem editing, git for git operations, host_repo for dispatcher-host repo
    access. Use [] only when none apply. The system validates the profile's actual
    configured tools/backend; descriptions do not prove capability.
  - Each child task body is what a fresh worker will read with no other
    context — be specific about goal, approach, and acceptance criteria.

When the task is genuinely a single unit of work (no useful decomposition),
return:

  {
    "fanout": false,
    "rationale": "<one sentence>",
    "title": "<tightened title>",
    "body":  "<concrete spec for a single worker>",
    "assignee": "<profile name from the roster, or null for default>"
  }

In that case the task stays as one work item, just with a tightened spec and
a concrete assignee. If no profile fits, use null and the system will route to
the default_assignee.

No preamble, no closing remarks, no code fences. Output only the JSON object.
"""


_USER_TEMPLATE = """Task id: {task_id}
Title: {title}
Body:
{body}

Available profiles (assignees you may pick from):
{roster}

Default assignee (used when no profile fits a task): {default_assignee}
"""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


@dataclass
class DecomposeOutcome:
    """Result of decomposing a single triage task."""

    task_id: str
    ok: bool
    reason: str = ""
    fanout: bool = False
    child_ids: list[str] | None = None
    new_title: Optional[str] = None


def _profile_author() -> str:
    """Mirror of ``hermes_cli.kanban._profile_author``."""
    return _specify_author("decomposer")


def _resolve_profile_from_cfg(cfg: dict, key: str, *, fallback: Optional[str] = None) -> str:
    """``kanban.<key>`` if it names an existing profile, else ``fallback``
    (the root task's own assignee) if that does, else the active default
    profile — so a task is never stranded for lack of an owner.
    ``orchestrator_profile`` owns the root after fan-out; ``default_assignee``
    catches children the decomposer can't route.

    The root's assignee sits before the active profile because the decomposer
    runs inside whatever profile hosts the dispatcher — an operator's
    credential-less incognito profile, say — and that profile must never
    silently become the owner of work the card was assigned away from (#114294).
    """
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    explicit = (kanban_cfg.get(key) or "").strip()
    for candidate in (explicit, (fallback or "").strip()):
        if candidate:
            try:
                if profiles_mod.profile_exists(candidate):
                    return candidate
            except Exception:
                pass
    try:
        return profiles_mod.get_active_profile_name() or "default"
    except Exception:
        return "default"


def _build_roster() -> tuple[list[dict], set[str]]:
    """``(roster_for_prompt, valid_assignee_names)``; entries are
    ``{name, description, has_description}``."""
    try:
        all_profiles = profiles_mod.list_profiles()
    except Exception as exc:
        logger.warning("decompose: failed to list profiles: %s", exc)
        return [], set()
    from hermes_cli.kanban_gate_only import gate_only_assignees
    gate_only = set(gate_only_assignees())  # only their admission gate assigns them, never the decomposer [daveey]
    all_profiles = [p for p in all_profiles if p.name not in gate_only]
    roster = []
    for p in all_profiles:
        desc = (p.description or "").strip()
        roster.append({
            "name": p.name,
            "description": desc or f"(no description; profile named {p.name!r})",
            "has_description": bool(desc),
        })
    return roster, {p.name for p in all_profiles}


def _format_roster(roster: list[dict], card_names: Optional[set[str]] = None) -> str:
    """``card_names`` (None = all): profiles that may own the card itself; the others are marked children-only."""
    if not roster:
        return "  (no profiles installed — decomposer cannot route work)"
    return "\n".join(
        f"  - {entry['name']}{'' if entry['has_description'] else ' ⚠ undescribed'}"
        f"{'' if card_names is None or entry['name'] in card_names else _CHILD_ONLY}: {entry['description']}"
        for entry in roster
    )


_CHILD_ONLY = " ⚠ no pool worker runs it: fanout children only, never this task itself"


def _normalize_assignee_choice(assignee: object, *, default_assignee: str, valid_names: set[str]) -> str:
    """A valid assignee, else ``default_assignee`` — promoted work is never
    left unassigned."""
    if not isinstance(assignee, str) or not assignee.strip():
        return default_assignee
    chosen = assignee.strip()
    return chosen if chosen in valid_names else default_assignee


@dataclass
class _Routing:
    """Config-derived routing context for one decomposition."""

    orchestrator: str
    default_assignee: str
    auto_promote: bool
    roster: list[dict]
    valid_names: set[str]
    # Profiles that may own the card ITSELF (single-task assignee, root after fan-out). Its placement is fixed
    # at creation, so an enrolled worker-first card keeps only profiles a pool worker runs; ``card_default``
    # is None when default_assignee is not one of them (keep the card's assignee). Fan-out children are inserted
    # unenrolled and run on this gateway, so they keep the whole roster. [daveey]
    card_names: Optional[set[str]] = None
    card_default: Optional[str] = None
    card_root: Optional[str] = None  # the root's owner after fan-out: orchestrator, else card_default

    def __post_init__(self) -> None:
        if self.card_names is None:  # built without a card: every profile may own it
            self.card_names = set(self.valid_names)
            self.card_default = self.default_assignee
            self.card_root = self.orchestrator


def _load_routing(*, root_assignee: Optional[str] = None, task: object = None) -> _Routing:
    from hermes_cli.config import load_config_readonly
    try:
        cfg = load_config_readonly()
    except Exception:  # decompose_task promises ok=False, never a raise, on config trouble
        cfg = {}
    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    roster, valid_names = _build_roster()
    routing = _Routing(
        orchestrator=_resolve_profile_from_cfg(cfg, "orchestrator_profile", fallback=root_assignee),
        default_assignee=_resolve_profile_from_cfg(cfg, "default_assignee", fallback=root_assignee),
        auto_promote=bool(kanban_cfg.get("auto_promote_children", True)),
        roster=roster,
        valid_names=valid_names,
    )
    from hermes_cli.kanban_profile_runnable import runnable_for
    routing.card_names = {n for n in valid_names if runnable_for(n, task)}
    routing.card_default = routing.default_assignee if runnable_for(routing.default_assignee, task) else None
    routing.card_root = routing.orchestrator if runnable_for(routing.orchestrator, task) else routing.card_default
    return routing


def _card_assignee(task_id: str, choice: object, routing: _Routing) -> Optional[str]:
    """The card's own new assignee: ``choice`` when it may own the card, else ``card_default``
    (None = leave the card's assignee unchanged)."""
    chosen = _normalize_assignee_choice(
        choice, default_assignee=routing.card_default or "", valid_names=routing.card_names or set(),
    )
    picked = choice.strip() if isinstance(choice, str) else ""
    if picked in routing.valid_names and picked not in (routing.card_names or set()):
        logger.info("decompose: task %s: no pool worker runs %r; assignee %r instead",
                    task_id, picked, chosen or "(unchanged)")
    return chosen or None


def _apply_single(task: kb.Task, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    """``fanout=false``: single-task spec promotion (same effect as specify)."""
    title_val, body_val = _title_body(parsed)
    assignee_val = None
    if not task.assignee:
        assignee_val = _card_assignee(task.id, parsed.get("assignee"), routing)
    if title_val is None and body_val is None:
        return DecomposeOutcome(task.id, False, "decomposer returned fanout=false with no title/body")
    try:
        with kbc.connect_closing() as conn:
            ok = kb.specify_triage_task(
                conn, task.id, title=title_val, body=body_val, assignee=assignee_val, author=author,
            )
    except ValueError as exc:
        return DecomposeOutcome(task.id, False, f"DB rejected assignment: {exc}")
    if not ok:
        return DecomposeOutcome(task.id, False, "task moved out of triage before promotion")
    return DecomposeOutcome(task.id, True, "single task (no fanout)", fanout=False, new_title=title_val)


def _clean_children(task_id: str, raw_tasks: list, routing: _Routing) -> tuple[list[dict], str]:
    """Validate/normalise the LLM's ``tasks`` list; ``(children, "")`` or ``([], reason)``.
    Unknown assignees route to the default; never assignee=None."""
    children: list[dict] = []
    for idx, entry in enumerate(raw_tasks):
        if not isinstance(entry, dict):
            return [], f"tasks[{idx}] is not an object"
        title = entry.get("title")
        if not isinstance(title, str) or not title.strip():
            return [], f"tasks[{idx}].title is missing or empty"
        body = entry.get("body")
        assignee = entry.get("assignee")
        chosen = _normalize_assignee_choice(
            assignee, default_assignee=routing.default_assignee, valid_names=routing.valid_names,
        )
        if isinstance(assignee, str) and assignee.strip() and assignee.strip() not in routing.valid_names:
            logger.info(
                "decompose: task %s child %d picked unknown assignee %r — "
                "routing to default_assignee %r",
                task_id, idx, assignee, routing.default_assignee,
            )
        parents = entry.get("parents") or []
        if not isinstance(parents, list):
            parents = []
        children.append({
            "title": title.strip()[:200],
            "body": body.strip() if isinstance(body, str) else "",
            "assignee": chosen,
            "requires": entry.get("requires"),
            # Drop non-int, out-of-range and self parent indices.
            "parents": [p for p in parents if isinstance(p, int) and 0 <= p < len(raw_tasks) and p != idx],
        })
    return children, ""


def _apply_fanout(task_id: str, parsed: dict, routing: _Routing, author: str) -> DecomposeOutcome:
    raw_tasks = parsed.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return DecomposeOutcome(task_id, False, "decomposer returned fanout=true with empty tasks list")
    children, reason = _clean_children(task_id, raw_tasks, routing)
    if reason:
        return DecomposeOutcome(task_id, False, reason)
    from hermes_cli.kanban_capabilities import RequiresDeclarationMissing, declaration_required
    try:
        with kbc.connect_closing() as conn:
            child_ids = decompose_triage_task(
                conn,
                task_id,
                root_assignee=routing.card_root,
                children=children,
                author=author,
                auto_promote=routing.auto_promote,
                require_declaration=declaration_required(),
            )
    except RequiresDeclarationMissing as exc:
        return DecomposeOutcome(task_id, False, f"{exc.code}: {exc}")
    except ValueError as exc:
        return DecomposeOutcome(task_id, False, f"DB rejected graph: {exc}")
    except Exception as exc:
        logger.exception("decompose: DB error on task %s", task_id)
        return DecomposeOutcome(task_id, False, f"DB error: {type(exc).__name__}")
    if child_ids is None:
        return DecomposeOutcome(task_id, False, "task already decomposed or moved out of triage")
    return DecomposeOutcome(
        task_id, True, f"decomposed into {len(child_ids)} children", fanout=True, child_ids=child_ids,
    )


def decompose_task(
    task_id: str,
    *,
    author: Optional[str] = None,
    timeout: Optional[int] = None,
    force: bool = False,
) -> DecomposeOutcome:
    """Decompose a triage task into a graph of child tasks. Expected failures
    (not in triage, no aux client, API error, malformed/empty reply) surface
    as ``ok=False``. A card that already ran or was escalated by a block loop
    is refused (:func:`kanban_db.decomposition_refusal`) unless ``force``."""
    task, reason = _load_triage_task(task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, reason)
    if not force:
        with kbc.connect_closing() as conn:
            refusal = kb.decomposition_refusal(conn, task_id)
        if refusal:
            return DecomposeOutcome(task_id, False, refusal)

    routing = _load_routing(root_assignee=task.assignee, task=task)
    user_prompt = _USER_TEMPLATE.format(
        **_task_prompt_fields(task),
        roster=_format_roster(routing.roster, routing.card_names),
        default_assignee=routing.default_assignee,
    )
    audit_author = author or _profile_author()
    outcome = DecomposeOutcome(task_id, False, "decomposer made no attempt")
    # One corrective re-ask when the graph was refused for an omitted ``requires``: the decomposer
    # is the agent that can correct it, so the error goes back to it, never to a human. [daveey]
    for attempt in range(2):
        raw, reason = _call_aux(
            "decompose", task_id, aux_task="kanban_decomposer", system=_SYSTEM_PROMPT,
            user=user_prompt, max_tokens=4000, timeout=timeout or 180, log=logger,
        )
        if raw is None:
            return DecomposeOutcome(task_id, False, reason)

        parsed = _extract_json_blob(raw, _FENCE_RE)
        if parsed is None:
            return DecomposeOutcome(task_id, False, "LLM returned malformed JSON")

        if not parsed.get("fanout"):
            return _apply_single(task, parsed, routing, audit_author)
        outcome = _apply_fanout(task_id, parsed, routing, audit_author)
        if outcome.ok or attempt or not str(outcome.reason).startswith("requires_declaration_missing"):
            return outcome
        user_prompt = (f"{user_prompt}\n\nYour previous answer was refused and nothing was created: "
                       f"{outcome.reason}\nReturn the complete JSON object again, with an explicit "
                       "\"requires\" list on EVERY child ([] only when none apply).")
    return outcome


def list_triage_ids(*, tenant: Optional[str] = None, skip_awaiting_human: bool = False,
                    min_age_seconds: int = 0, now: Optional[float] = None) -> list[str]:
    """Return the triage task ids that are decomposition candidates.

    Every sweep (the dispatcher's auto-decompose and ``decompose --all``) drops
    cards that already ran or that ``block_loop_detected`` escalated, even after
    a human comments on them (:func:`kanban_db.decomposition_refusal`); those wait
    for ``promote``/``unblock``. ``skip_awaiting_human`` is kept for callers and
    is now implied. ``min_age_seconds`` drops cards created less than that long
    ago (the dispatcher's ``kanban.auto_decompose_min_age_seconds`` grace window,
    so a creator can still ``kanban_schedule`` a triage hold); 0 keeps all. [daveey]
    """
    del skip_awaiting_human
    cutoff = (time.time() if now is None else now) - max(int(min_age_seconds or 0), 0)
    # Read-only, every dispatcher tick per board: a kept connection, not an open per call (design t_01a1d563 I).
    from hermes_cli.kanban_kept_reader import reading
    with reading() as conn:
        rows = kb.list_tasks(conn, status="triage", tenant=tenant, limit=1000)
        if min_age_seconds:
            # An unparseable stamp (None) stays a candidate: excluding it would strand the card.
            rows = [row for row in rows if row.created_at is None or row.created_at <= cutoff]
        rows = [row for row in rows if kb.decomposition_refusal(conn, row.id) is None]
    return [row.id for row in rows]
