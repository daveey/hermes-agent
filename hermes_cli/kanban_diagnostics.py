"""Kanban diagnostics — structured, actionable distress signals for tasks.

A ``Diagnostic`` carries a **kind** (canonical code the UI/tests match on), a
**severity**, title/detail text, and **actions** the dashboard renders as
buttons and the CLI as hints. Rules are stateless and read-only over
(task, events, runs, optional graph); callers compute on demand. Only
operator-fixable signals (not a one-off provider 502); every diagnostic has a
recovery action and auto-clears when the failure mode resolves.
"""

from __future__ import annotations

from types import MappingProxyType
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional
import json
import time
from hermes_cli.kanban_diagnostic_history import EventHistory


# Least → most urgent; sorted outputs put critical first.
SEVERITY_ORDER = ("warning", "error", "critical")


def severity_at_or_above(severity: Optional[str], threshold: Optional[str]) -> bool:
    """Return True when ``severity`` meets or exceeds ``threshold``."""
    if threshold is None:
        return True
    if severity not in SEVERITY_ORDER or threshold not in SEVERITY_ORDER:
        return False
    return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(threshold)


@dataclass
class DiagnosticAction:
    """A recovery action. ``kind`` drives rendering: ``reclaim``/``reassign``
    POST to /tasks/:id/*; ``unblock`` PATCHes status to ready; ``cli_hint``
    shows ``payload.command``; ``open_docs`` links ``payload.url``; ``comment``
    nudges the operator. ``suggested=True`` = recommended first step."""

    kind: str
    label: str
    payload: dict = field(default_factory=dict)
    suggested: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Diagnostic:
    """One active distress signal on a task."""

    kind: str
    severity: str  # "warning" | "error" | "critical"
    title: str
    detail: str
    actions: list[DiagnosticAction] = field(default_factory=list)
    first_seen_at: int = 0
    last_seen_at: int = 0
    count: int = 1
    run_id: Optional[int] = None  # None = task-wide
    data: dict = field(default_factory=dict)  # structured payload for the UI

    def to_dict(self) -> dict:
        return asdict(self)


# --- Rule helpers ---

def _task_field(task, name, default=None):
    """Read a field from a sqlite3.Row, a kanban_db.Task dataclass, or a dict."""
    if task is None:
        return default
    try:
        # Immutable history mappings and ordinary dictionaries already have O(1)
        # membership; Row.keys() allocates and linearly scans all task columns.
        if isinstance(task, (dict, MappingProxyType)):
            if name in task:
                return task[name]
        elif hasattr(task, "keys") and name in task.keys():
            return task[name]
    except Exception:
        pass
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def _human_request(task) -> dict | None:
    """Read canonical current evidence, never resurrect an ask from older events."""
    from hermes_cli.kanban_human_request import validate_human_request

    value = _task_field(task, "human_request")
    try:
        if isinstance(value, str):
            value = json.loads(value)
        return validate_human_request(value, kind=_task_field(task, "block_kind"))
    except (ValueError, TypeError):
        return None


def _parse_payload(ev) -> dict:
    """Tolerate event.payload being either a dict or a JSON string."""
    p = _task_field(ev, "payload", None)
    if isinstance(p, dict):
        return p
    if isinstance(p, str):
        try:
            return json.loads(p) or {}
        except Exception:
            return {}
    return {}


def _event_kind(ev) -> str:
    return _task_field(ev, "kind", "") or ""


def _event_ts(ev) -> int:
    return int(_task_field(ev, "created_at", 0) or 0)


def _first_field(task, primary: str, legacy: str, default=None):
    """``task[primary]`` unless it is None, else ``task[legacy]`` (old DB rows)."""
    v = _task_field(task, primary, None)
    return v if v is not None else _task_field(task, legacy, default)


def _latest_event_ts(events: Iterable[Any], kinds: set[str] | frozenset[str]) -> int:
    """Max ``created_at`` over events whose kind is in ``kinds`` (0 if none)."""
    if isinstance(events, EventHistory):
        return events.latest_ts(kinds)
    return max([0, *(_event_ts(ev) for ev in events if _event_kind(ev) in kinds)])


def _latest_gave_up_is_terminal_provider(events: Iterable[Any]) -> bool:
    """True when the most recent breaker trip was a terminal provider error (credential
    revoked, model gone) and nothing has resumed the task since."""
    if isinstance(events, EventHistory):
        return events.terminal_provider
    for ev in reversed(list(events)):
        kind = _event_kind(ev)
        if kind == "gave_up":
            return bool(_parse_payload(ev).get("terminal_provider"))
        if kind in {"unblocked", "promoted", "completed", "claimed"}:
            return False
    return False


def _cli_hint(label: str, command: str, *, suggested: bool = False) -> DiagnosticAction:
    return DiagnosticAction(kind="cli_hint", label=label, payload={"command": command},
                            suggested=suggested)


def _log_hint_action(task_id: str) -> DiagnosticAction:
    cmd = f"hermes kanban log {task_id}"
    return _cli_hint(f"Check logs: {cmd}", cmd, suggested=True)


def _error_snippet(last_err) -> str:
    """First 500 chars of the error (with ellipsis), or "" when absent."""
    err_text = (last_err or "").strip() if last_err else ""
    return err_text[:500] + ("…" if len(err_text) > 500 else "") if err_text else ""


def _active_hallucination_events(events: Iterable[Any], kind: str) -> list[Any]:
    """Events of ``kind`` with no ``completed``/``edited`` event strictly after
    them. Requires id-sorted (arrival-order) input, which the DB provides."""
    if isinstance(events, EventHistory):
        if kind == "completion_blocked_hallucination":
            return list(events.active_cards)
        if kind == "suspected_hallucinated_references":
            return list(events.active_refs)
    active: list[Any] = []
    for ev in events:
        k = _event_kind(ev)
        if k in {"completed", "edited"}:
            active.clear()
        elif k == kind:
            active.append(ev)
    return active


def _unique_payload_ids(hits: list[Any], key: str) -> list[str]:
    """Ordered, de-duplicated ``payload[key]`` entries across ``hits``."""
    out: list[str] = []
    for ev in hits:
        for pid in _parse_payload(ev).get(key, []) or []:
            if pid not in out:
                out.append(pid)
    return out


def _generic_recovery_actions(task: Any, *, running: bool) -> list[DiagnosticAction]:
    """Baseline recovery primitives every diagnostic can fall back on."""
    out: list[DiagnosticAction] = []
    if running:
        out.append(DiagnosticAction(kind="reclaim", label="Reclaim task", payload={}))
    out.append(DiagnosticAction(
        kind="reassign", label="Reassign to different profile", payload={"reclaim_first": running},
    ))
    return out


def _is_running(task) -> bool:
    return _task_field(task, "status") == "running"


def _runs_newest_first(runs) -> list[Any]:
    # reversed(sorted()) not sorted(reverse=True): equal ids must keep the
    # last-listed run first.
    return list(reversed(sorted(runs, key=lambda r: _task_field(r, "id", 0))))


# --- Rule implementations ---

# Each rule: (task, events, runs, now_ts, config) -> list[Diagnostic].
# ``events``/``runs`` are kanban_db rows/dataclasses or same-shaped dicts.

RuleFn = Callable[[Any, list[Any] | tuple, list[Any], int, dict], list[Diagnostic]]


def _aux_slot_explicit(slot: Any) -> bool:
    """True if the aux slot was user-configured: provider other than "auto",
    or any of model/base_url/api_key set (the default falls through to the
    main model)."""
    if not isinstance(slot, dict):
        return False
    provider = str(slot.get("provider") or "").strip().lower()
    if provider and provider != "auto":
        return True
    return any(str(slot.get(key) or "").strip() for key in ("model", "base_url", "api_key"))


def _main_model_visible(raw_config: Any) -> bool:
    """Best-effort "a main model is configured" from the raw config dict (the
    dashboard process may not share CLI runtime state). Unprovable => False,
    which errs toward NOT firing the diagnostic."""
    if not isinstance(raw_config, dict):
        return False
    model_cfg = raw_config.get("model")
    if isinstance(model_cfg, dict):
        provider = str(model_cfg.get("provider") or "").strip()
        model = str(
            model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name") or ""
        ).strip()
        return bool(provider and model)
    return bool(str(model_cfg or "").strip())


def triage_aux_status(config: Optional[dict]) -> Optional[dict]:
    """Report whether the triage aux paths look configured: ``{auto_decompose,
    decomposer_explicit, specifier_explicit, main_model_visible}``. ``None``
    when no config context is present (keeps low-level callers/tests silent)."""
    if not isinstance(config, dict):
        return None
    explicit = config.get("triage_aux_status")
    if isinstance(explicit, dict):
        return explicit

    aux = config.get("auxiliary")
    kanban_cfg = config.get("kanban") if isinstance(config.get("kanban"), dict) else {}
    # No auxiliary/kanban/model keys at all => a low-level caller passing {}.
    if not isinstance(aux, dict) and not kanban_cfg and "model" not in config:
        return None
    aux = aux if isinstance(aux, dict) else {}
    return {
        # ``auto_decompose`` defaults to True per kanban DEFAULT_CONFIG.
        "auto_decompose": bool(kanban_cfg["auto_decompose"]) if "auto_decompose" in kanban_cfg else True,
        "decomposer_explicit": _aux_slot_explicit(aux.get("kanban_decomposer")),
        "specifier_explicit": _aux_slot_explicit(aux.get("triage_specifier")),
        "main_model_visible": _main_model_visible(config),
    }


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _rule_hallucinated_cards(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A worker's kanban_complete named created_cards that don't exist / weren't
    its own; the completion was blocked. Clears on a later completion/edit."""
    hits = _active_hallucination_events(events, "completion_blocked_hallucination")
    if not hits:
        return []
    actions = [DiagnosticAction(kind="comment", label="Add a comment explaining what to do",
                                suggested=False)]
    actions += _generic_recovery_actions(task, running=_is_running(task))
    return [Diagnostic(
        kind="hallucinated_cards", severity="error",
        title="Worker claimed cards that don't exist",
        detail="The completing worker declared created_cards that either didn't exist or weren't "
               "created by its profile. The completion was blocked and the task stayed in its prior "
               "state. Usually means the worker hallucinated ids instead of capturing return values "
               "from kanban_create.",
        actions=actions,
        first_seen_at=_event_ts(hits[0]), last_seen_at=_event_ts(hits[-1]), count=len(hits),
        data={"phantom_ids": _unique_payload_ids(hits, "phantom_cards")},
    )]


# (primary_slot, fallback_slot, primary_desc, detail_path) keyed by auto_decompose.
_TRIAGE_SLOTS = {
    True: (
        "auxiliary.kanban_decomposer", "auxiliary.triage_specifier", "decomposer",
        "Auto-decompose is on, so the dispatcher needs auxiliary.kanban_decomposer (with "
        "auxiliary.triage_specifier as a fallback for non-fan-out tasks).",
    ),
    False: (
        "auxiliary.triage_specifier", "auxiliary.kanban_decomposer", "specifier",
        "Auto-decompose is off, so triage tasks need "
        "`hermes kanban specify`, which uses auxiliary.triage_specifier.",
    ),
}


def _rule_triage_aux_unavailable(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A triage task can't leave triage without a usable aux model. With
    auto-decompose on the primary slot is ``auxiliary.kanban_decomposer``
    (specifier as fallback); off, it is ``auxiliary.triage_specifier``. The
    default ``provider: auto`` falls back to the main model, so this fires only
    when the slot isn't explicit AND no main model is visible. Requires config
    context ({} keeps it silent)."""
    if _task_field(task, "status") != "triage":
        return []
    status = triage_aux_status(cfg)
    if status is None:
        return []

    auto_decompose = bool(status.get("auto_decompose"))
    main_visible = bool(status.get("main_model_visible"))
    decomposer_explicit = bool(status.get("decomposer_explicit"))
    specifier_explicit = bool(status.get("specifier_explicit"))
    primary_slot, fallback_slot, primary_desc, detail_path = _TRIAGE_SLOTS[auto_decompose]
    primary_explicit, fallback_explicit = (
        (decomposer_explicit, specifier_explicit) if auto_decompose
        else (specifier_explicit, decomposer_explicit)
    )
    if primary_explicit or main_visible:
        return []

    task_id = _task_field(task, "id") or "<task_id>"
    actions = [_cli_hint(
        f"Configure {primary_slot}", f"hermes config set {primary_slot}.provider auto", suggested=True,
    )]
    if not fallback_explicit and not main_visible:
        actions.append(_cli_hint(
            f"Or configure fallback {fallback_slot}", f"hermes config set {fallback_slot}.provider auto",
        ))
    if not auto_decompose:
        cmd = f"hermes kanban specify {task_id}"
        actions.append(_cli_hint(f"Specify manually: {cmd}", cmd))

    return [Diagnostic(
        kind="triage_aux_unavailable", severity="warning",
        title=f"Triage {primary_desc} has no usable model",
        detail=f"This task is still in triage and no working auxiliary model is visible to the "
               f"dispatcher. {detail_path} The default slot uses `provider: auto` which falls back to "
               f"the main model, but no main model is configured either. Configure the slot directly "
               f"or set a main model so the auto fallback can take over.",
        actions=actions,
        first_seen_at=now, last_seen_at=now, count=1,
        data={"task_id": task_id, "auto_decompose": auto_decompose,
              "primary_slot": primary_slot, "main_model_visible": main_visible},
    )]


def _rule_prose_phantom_refs(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Advisory: the completion summary mentions ``t_<hex>`` ids that don't
    resolve. Warning only; clears on a later clean completion."""
    hits = _active_hallucination_events(events, "suspected_hallucinated_references")
    if not hits:
        return []
    # Re-resolve at read time: ids that were unknown when the summary landed may be real cards on another
    # gateway (hub replica), so old false warnings clear. Unknown/stale replica = don't flag.
    from hermes_cli.kanban_db import unresolved_after_replica
    phantom = unresolved_after_replica(_unique_payload_ids(hits, "phantom_refs"))
    if not phantom:
        return []
    return [Diagnostic(
        kind="prose_phantom_refs", severity="warning",
        title="Completion summary references unknown task ids",
        detail="The completion summary mentions task ids that don't resolve on this board or in the hub replica "
               "of other gateways. "
               "The completion itself succeeded, but downstream consumers parsing the summary may be "
               "pointed at cards that never existed.",
        actions=_generic_recovery_actions(task, running=_is_running(task)),
        first_seen_at=_event_ts(hits[0]), last_seen_at=_event_ts(hits[-1]), count=len(hits),
        data={"phantom_refs": phantom},
    )]


def _failure_threshold(cfg: dict) -> Any:
    """``failure_threshold`` with the legacy ``spawn_failure_threshold`` alias."""
    return cfg.get("failure_threshold", cfg.get("spawn_failure_threshold", 3))


_OUTCOME_LABELS = {"spawn_failed": "spawn", "timed_out": "timeout", "crashed": "crash",
                   "oom_killed": "OOM kill", "signaled": "signal kill"}


def _rule_repeated_failures(task, events, runs, now, cfg) -> list[Diagnostic]:
    """``consecutive_failures`` >= cfg["failure_threshold"] (legacy key
    ``spawn_failure_threshold``), regardless of failure mode — the kernel keeps
    retrying and the operator must intervene. Runtime callers derive the
    threshold from ``kanban.failure_limit`` so it doesn't lag the breaker.

    Exempt: done/archived (a manual done ends no run, so the streak is history)
    and running (a retry in flight must not read as a current failure; re-fires
    if it fails too)."""
    if _task_field(task, "status") in ("done", "archived", "running"):
        return []
    threshold = _positive_int(_failure_threshold(cfg), 3)
    failure_limit = _positive_int(cfg.get("failure_limit"), threshold)
    failures = _first_field(task, "consecutive_failures", "spawn_failures", 0)
    # A terminal provider error (credential revoked, model gone) blocks the card after ONE
    # attempt, below any threshold; it still needs an operator, so diagnose it now.
    terminal_trip = _latest_gave_up_is_terminal_provider(events)
    if not terminal_trip and (failures is None or failures < threshold):
        return []
    failures = failures or 0
    last_err = _first_field(task, "last_failure_error", "last_spawn_error")
    assignee = _task_field(task, "assignee")

    # Most recent failure outcome makes the title/action specific.
    most_recent_outcome = next(
        (oc for oc in (_task_field(r, "outcome") for r in _runs_newest_first(runs))
         if oc in _OUTCOME_LABELS),
        None,
    )

    actions: list[DiagnosticAction] = []
    if most_recent_outcome == "spawn_failed" and assignee and assignee != "default":
        # Spawn is failing specifically — profile setup issue.
        doctor, auth = f"hermes -p {assignee} doctor", f"hermes -p {assignee} auth"
        actions.append(_cli_hint(f"Verify profile: {doctor}", doctor, suggested=True))
        actions.append(_cli_hint(f"Fix profile auth: {auth}", auth))
    elif most_recent_outcome in {"timed_out", "crashed", "oom_killed", "signaled"}:
        # Worker got off the ground but died: logs diagnose, reclaim/reassign recover.
        task_id = _task_field(task, "id")
        if task_id:
            actions.append(_log_hint_action(task_id))
    actions.extend(_generic_recovery_actions(task, running=_is_running(task)))

    severity = "critical" if failures >= threshold * 2 else "error"
    err_snippet = _error_snippet(last_err)
    outcome_label = _OUTCOME_LABELS.get(most_recent_outcome or "", "failure")
    if terminal_trip:
        title = "Provider rejected this profile's credential or model — blocked after one attempt"
        detail = (
            f"The worker's provider call failed with an error a retry cannot fix (revoked or invalid "
            f"API key, model not found), so the dispatcher blocked the task instead of spending the "
            f"{failure_limit}-attempt retry budget on it. Full last error:\n\n{err_snippet}\n\n"
            f"Fix the assignee profile's provider credentials/model, then unblock the task."
        )
    elif err_snippet:
        title = f"Agent {outcome_label} x{failures}: {err_snippet.splitlines()[0][:160]}"
        detail = (
            f"This task has failed {failures} times in a row (most recent: {outcome_label}). Full "
            f"last error:\n\n{err_snippet}\n\nThe dispatcher circuit breaker is configured for "
            f"{failure_limit} consecutive non-success attempts. Fix the root cause and reclaim or "
            f"unblock the task to retry."
        )
    else:
        title = f"Agent {outcome_label} x{failures} (no error recorded)"
        detail = (
            f"This task has failed {failures} times in a row (most recent: {outcome_label}) but no "
            f"error text was captured. Check the suggested command or the worker log."
        )
    return [Diagnostic(
        kind="repeated_failures", severity=severity,
        title=title, detail=detail, actions=actions,
        first_seen_at=now, last_seen_at=now, count=failures,
        data={
            "consecutive_failures": failures,
            "most_recent_outcome": most_recent_outcome,
            "last_error": last_err,
            "failure_threshold": threshold,
            "failure_limit": failure_limit,
        },
    )]


def _rule_repeated_crashes(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Trailing run outcomes show >= cfg["crash_threshold"] (default 2)
    consecutive ``crashed`` with no ``completed``/``reclaimed`` between. Fires
    earlier than ``repeated_failures`` for a crash-specific heads-up and
    suppresses itself when the unified rule is about to fire.

    Exempt: done/archived (a manual done appends no completed run, so the
    streak would be permanent) and running (an in-flight run has no outcome
    and wouldn't break the scan)."""
    if _task_field(task, "status") in ("done", "archived", "running"):
        return []
    # Unified rule will catch this — let it handle to avoid double fire.
    if (_task_field(task, "consecutive_failures", 0) or 0) >= int(_failure_threshold(cfg)):
        return []

    threshold = int(cfg.get("crash_threshold", 2))
    # Count trailing consecutive 'crashed' outcomes; a success (or manual
    # reclaim) breaks the streak, other outcomes neither count nor break it.
    consecutive = 0
    last_err = None
    for r in _runs_newest_first(runs):
        outcome = _task_field(r, "outcome")
        if outcome in {"crashed", "oom_killed", "signaled"}:
            consecutive += 1
            if last_err is None:
                last_err = _task_field(r, "error")
        elif outcome in {"completed", "reclaimed"}:
            break
    if consecutive < threshold:
        return []
    task_id = _task_field(task, "id")
    actions: list[DiagnosticAction] = []
    if task_id:
        actions.append(_log_hint_action(task_id))
    actions.extend(_generic_recovery_actions(task, running=_is_running(task)))
    severity = "critical" if consecutive >= threshold * 2 else "error"
    # Error up-front so operators see WHAT broke without opening the logs.
    err_snippet = _error_snippet(last_err)
    if err_snippet:
        title = f"Agent crashed {consecutive}x: {err_snippet.splitlines()[0][:160]}"
        detail = (
            f"The last {consecutive} runs crashed or were killed. "
            f"Full last error:\n\n{err_snippet}"
        )
    else:
        title = f"Agent crashed {consecutive}x (no error recorded)"
        detail = (
            f"The last {consecutive} runs crashed or were killed but "
            f"no error text was captured. Check the worker log for more."
        )
    return [Diagnostic(
        kind="repeated_crashes", severity=severity,
        title=title, detail=detail, actions=actions,
        first_seen_at=now, last_seen_at=now, count=consecutive,
        data={"consecutive_crashes": consecutive, "last_error": last_err},
    )]


def _rule_review_dependency_deadlock(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Legacy review handoff starving children: the implementation is
    sticky-blocked with a ``review-required:`` reason while todo children wait
    for it to be terminal. Graph-aware; deliberately mutates nothing."""
    if _task_field(task, "status") != "blocked":
        return []
    latest_block = (events.last_block if isinstance(events, EventHistory) else
                    next((ev for ev in reversed(list(events)) if _event_kind(ev) == "blocked"), None))
    if latest_block is None:
        return []
    reason = str(_parse_payload(latest_block).get("reason") or "").strip()
    if not reason.lower().startswith("review-required:"):
        return []

    graph = cfg.get("_graph")
    if not isinstance(graph, dict):
        return []
    waiting_children = [
        child for child in (graph.get("children") or [])
        if isinstance(child, dict) and child.get("status") == "todo"
    ]
    if not waiting_children:
        return []

    task_id = str(_task_field(task, "id") or "")
    child_ids = [str(child.get("id")) for child in waiting_children if child.get("id")]
    actions: list[DiagnosticAction] = []
    if task_id:
        actions.append(_cli_hint(
            "Complete the finished implementation phase", f"hermes kanban complete {task_id}",
            suggested=True,
        ))
    if task_id and child_ids:
        actions.append(_cli_hint(
            "Or unlink the incorrectly gated reviewer", f"hermes kanban unlink {task_id} {child_ids[0]}",
        ))

    blocked_at = _event_ts(latest_block) or now
    return [Diagnostic(
        kind="review_dependency_deadlock", severity="error",
        title=f"Review handoff blocks {len(child_ids)} dependent task(s)",
        detail="This implementation is sticky-blocked for review while its downstream task(s) require "
               "the implementation to be done or archived before they can run. Complete the finished "
               "phase, unlink the incorrect dependency, or migrate this workflow to the first-class "
               "review lifecycle.",
        actions=actions,
        first_seen_at=blocked_at, last_seen_at=blocked_at, count=len(child_ids),
        data={"blocked_parent_id": task_id, "waiting_child_ids": child_ids, "block_reason": reason},
    )]


def _rule_running_with_open_parents(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A ``running`` card with a direct parent that is not ``done``/``archived``:
    the dependency gate is not holding it (the parent reopened mid-run, or the
    edge predates the running-child refusal) and ``kanban_complete`` will be
    refused until the parents finish. Graph-aware; mutates nothing."""
    if _task_field(task, "status") != "running":
        return []
    graph = cfg.get("_graph")
    if not isinstance(graph, dict):
        return []
    open_parents = [
        parent for parent in (graph.get("parents") or [])
        if isinstance(parent, dict) and parent.get("id")
        and parent.get("status") not in ("done", "archived")
    ]
    if not open_parents:
        return []
    task_id = str(_task_field(task, "id") or "")
    parent_ids = [str(parent["id"]) for parent in open_parents]
    seen_at = int(_task_field(task, "started_at", default=0) or 0) or now
    return [Diagnostic(
        kind="running_with_open_parents", severity="warning",
        title=f"Running while {len(parent_ids)} parent(s) are not done",
        detail="This card is running concurrently with a parent it declares a dependency on, so the "
               "parent's work is not serialised ahead of it and completion will be refused until every "
               "parent is done or archived. Finish the parent, or unlink the edge if it was never meant "
               "to gate this run.",
        actions=[_cli_hint("Unlink the parent that should not gate this run",
                           f"hermes kanban unlink {parent_ids[0]} {task_id}")],
        first_seen_at=seen_at, last_seen_at=now, count=len(parent_ids),
        data={"open_parents": [{"id": p["id"], "status": p.get("status")} for p in open_parents]},
    )]


def _rule_stuck_in_blocked(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Blocked for >= cfg["blocked_stale_hours"] (default 24) with no comment
    or unblock since the last ``blocked`` event."""
    hours = float(cfg.get("blocked_stale_hours", 24))
    if _task_field(task, "status") != "blocked":
        return []
    last_blocked_ts = _latest_event_ts(events, {"blocked"})
    if last_blocked_ts == 0:
        return []
    age_hours = (now - last_blocked_ts) / 3600.0
    if age_hours < hours:
        return []
    # Any comment / unblock after the block breaks the "stale" signal.
    if any(_event_kind(ev) in {"commented", "unblocked"} and _event_ts(ev) > last_blocked_ts
           for ev in events):
        return []
    return [Diagnostic(
        kind="stuck_in_blocked", severity="warning",
        title=f"Task has been blocked for {int(age_hours)}h",
        detail=f"This task transitioned to blocked {int(age_hours)}h ago and has had no comments or "
               f"unblock attempts since. Owner: {_task_field(task, 'assignee') or 'unassigned'}. "
               + ("Review the explicit human request and answer it."
                  if _human_request(task) else "Operational blocker: inspect the cause and record a recovery plan."),
        actions=[DiagnosticAction(kind="comment", label="Record a response or recovery plan",
                                  suggested=True)],
        first_seen_at=last_blocked_ts, last_seen_at=last_blocked_ts, count=1,
        data={"blocked_at": last_blocked_ts, "age_hours": round(age_hours, 1)},
    )]


def _rule_block_unblock_cycling(task, events, runs, now, cfg) -> list[Diagnostic]:
    """>= cfg["block_cycle_threshold"] (default 3) blocked-after-unblocked
    cycles within cfg["block_cycle_window_seconds"] (default 24h). Complements
    ``_rule_stuck_in_blocked``, whose timer any unblock resets, so fast cyclers
    are invisible to it.

    ``_rule_stuck_in_blocked`` resets its timer on any ``commented`` / ``unblocked`` event, so a task that
    cycles every few minutes is invisible to it regardless of how many times it cycles (#29747 gap 1). This
    rule complements that one by counting block→unblock cycles in a sliding window.
    """
    threshold = _positive_int(cfg.get("block_cycle_threshold"), 3)
    window_seconds = float(cfg.get("block_cycle_window_seconds", 24 * 3600))
    cycle_cutoff = now - window_seconds

    # Walk in id (arrival) order — created_at alone can't order events that
    # share a second. A blocked event after >= 1 unblocked since the last
    # counted cycle is a new cycle.
    cycles = 0
    seen_unblock_since_last_cycle = False
    initial_blocked_ts = 0
    last_cycle_blocked_ts = 0
    cycle_inputs = (events.cycle_inputs if isinstance(events, EventHistory)
                    else ((_event_ts(ev), _event_kind(ev)) for ev in events))
    for ts, kind in cycle_inputs:
        if ts < cycle_cutoff:
            continue
        if kind == "blocked":
            if initial_blocked_ts == 0:
                initial_blocked_ts = ts
            if seen_unblock_since_last_cycle:
                cycles += 1
                last_cycle_blocked_ts = ts
                seen_unblock_since_last_cycle = False
        elif kind == "unblocked":
            seen_unblock_since_last_cycle = True

    if cycles < threshold:
        return []

    task_id = _task_field(task, "id")
    actions: list[DiagnosticAction] = []
    if task_id:
        cmd = f"hermes kanban events {task_id}"
        actions.append(_cli_hint(f"Check block reasons: {cmd}", cmd, suggested=True))
    return [Diagnostic(
        kind="block_unblock_cycling", severity="warning",
        title=f"Task block→unblock cycled {cycles}x in {int(window_seconds/3600)}h",
        detail=f"This task has been blocked {cycles} times after being unblocked, suggesting the "
               f"unblock is not addressing the root cause and the worker keeps hitting the same wall. "
               f"Review the block reasons in the event history; a different intervention (reassign, "
               f"change scope, archive) may be needed.",
        actions=actions,
        first_seen_at=int(initial_blocked_ts) if initial_blocked_ts else int(now),
        last_seen_at=int(last_cycle_blocked_ts) if last_cycle_blocked_ts else int(now),
        count=cycles,
        data={"cycles": cycles, "window_seconds": int(window_seconds)},
    )]


# Events after which a now-ready, unclaimed task must have (re-)entered ready:
# the classic ready transitions plus every claim/park/wake on the way back, and a run lost
# to a gateway restart (booked ``owner_restart_lost``, not ``reclaimed``: without it an 80-min
# run read as 80 min ready, hermes-hub 2026-11-10). Other run-ending requeues are bounded by
# the run's ``ended_at`` (``_last_run_end_ts``), never by kinds a pre-claim check may write. [daveey]
# Compatibility export for diagnostic consumers; the registry lives outside this facade.
from hermes_cli.kanban_ready_events import READY_WAIT_FLOOR_KINDS as _READY_WAIT_FLOOR_KINDS


def _last_run_end_ts(runs: Iterable[Any]) -> int:
    """Latest ``ended_at`` over ``runs`` (0 if none): a card is never unclaimed-ready while a run is open."""
    from hermes_cli import kanban_db as kb
    ends = [kb._to_epoch(_task_field(r, "ended_at", None)) for r in (runs or [])]
    return int(max([0, *(e for e in ends if e)]))


def _last_ready_entry_ts(events: Iterable[Any], runs: Iterable[Any] = ()) -> int:
    """Latest ready-entry floor; a ``continued`` checkpoint counts only when it resumed straight to ready.
    The latest run's end bounds it too, whatever event (if any) the requeue left.

    A Board grant returned unused (``kanban_unused_grant``) is a reservation, not execution: its ``claimed``
    event and its run's end never reset the clock, so a card whose grants keep bouncing keeps its real age
    (default/t_01a1c6c56a99c3e1, 2026-11-10: ready@1794369068 hid a wait since 1794351914). Actual execution,
    loss and ready transitions still count. [daveey]"""
    if isinstance(events, EventHistory):
        return events.ready_floor(runs)
    from hermes_cli.kanban_unused_grant import unused_grant_run_ids
    events, runs = list(events), list(runs)
    unused = unused_grant_run_ids(runs, events)
    if unused:
        events = [ev for ev in events if _task_field(ev, "run_id", None) not in unused]
        runs = [r for r in runs if _task_field(r, "id", None) not in unused]
    continued_ready = [_event_ts(ev) for ev in events
                       if _event_kind(ev) == "continued" and _parse_payload(ev).get("status") == "ready"]
    return max([_latest_event_ts(events, _READY_WAIT_FLOOR_KINDS), *continued_ready, _last_run_end_ts(runs)])


def _guard_clock(events: Iterable[Any], runs: Iterable[Any], cfg,
                 since: int = 0) -> tuple[Optional[str], Optional[int]]:
    """``(guard reason, hold-until epoch)`` from the latest ``respawn_guarded`` event of this ready episode
    (at or after ``since``); the epoch only for a run-end clock hold (owner-restart / rate-limit /
    infrastructure cooldown), computed like ``kanban_db_dispatch_clock.guard_hold_until`` (quota-episode
    backoffs are not modelled). Advisory: the live verdict is the dispatcher's."""
    guarded = [ev for ev in (events or []) if _event_kind(ev) == "respawn_guarded" and _event_ts(ev) >= since]
    if not guarded:
        return None, None
    reason = _parse_payload(max(guarded, key=_event_ts)).get("reason")
    reason = str(reason) if reason else None
    end = _last_run_end_ts(runs)
    if not end or reason not in ("owner_restart_cooldown", "rate_limit_cooldown", "infrastructure_cooldown"):
        return reason, None
    from hermes_cli import kanban_db as kb
    if reason == "owner_restart_cooldown":
        from hermes_cli.kanban_db_dispatch import OWNER_RESTART_COOLDOWN_SECONDS
        cooldown = cfg.get("owner_restart_cooldown_seconds") or kb._env_int(
            "HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", OWNER_RESTART_COOLDOWN_SECONDS)
    else:
        cooldown = cfg.get("rate_limit_cooldown_seconds") or kb._resolve_rate_limit_cooldown_seconds()
    return reason, end + int(cooldown)


def _foreign_machines(task, cfg) -> list[str]:
    """``machine:<m>`` runs_on labels naming a machine other than this hub (cfg ``own_machine``)."""
    raw = _task_field(task, "runs_on")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    own = str(cfg.get("own_machine", "hub"))
    return [str(l).split(":", 1)[1] for l in (raw or [])
            if str(l).startswith("machine:") and str(l).split(":", 1)[1] != own]


def _episode_skip(events) -> Mapping:
    """Payload of the current ready episode's latest ``dispatch_skipped`` (``kanban_dispatch_skip``), or {} when a
    later episode event (or none) ends it. Same episode bounds as its reader."""
    if isinstance(events, EventHistory):
        return events.episode_skip
    from hermes_cli.kanban_dispatch_skip import EVENT, _episode_kinds
    bounds = _episode_kinds() | {EVENT}
    ordered = sorted(events or [], key=lambda ev: (_event_ts(ev), _task_field(ev, "id", 0) or 0), reverse=True)
    for ev in ordered:
        if _event_kind(ev) not in bounds:
            continue
        return _parse_payload(ev) if _event_kind(ev) == EVENT else {}
    return {}


def _pinned_quota_hold_until(events, now) -> Optional[int]:
    """Reset time when the current ready episode's latest ``dispatch_skipped`` is a ``pinned_quota_wall``
    hold still in the future (``kanban_dispatch_skip``); else None."""
    from hermes_cli.kanban_dispatch_skip import PINNED_QUOTA_WALL
    payload = _episode_skip(events)
    until = payload.get("until")
    if payload.get("reason") == PINNED_QUOTA_WALL and isinstance(until, (int, float)) and until > now:
        return int(until)
    return None


def _llm_paced_hold(events, now) -> Optional[dict]:
    """The episode's latest skip when it is an LLM quota-pacing hold (``llm_paced``) whose pool reset, if it named
    one, is still ahead; else None. A paced pool names no reset: the dispatcher re-reads it every interval."""
    from hermes_cli.kanban_dispatch_skip import LLM_PACED
    payload = _episode_skip(events)
    until = payload.get("until")
    if payload.get("reason") != LLM_PACED or (isinstance(until, (int, float)) and until <= now):
        return None
    return payload


def _ready_hold_reason(task, events, cfg, now=None) -> Optional[str]:
    """Last observed guard or current foreign-machine pin, not a live admission verdict.

    Capacity checks can precede pickup/guard evaluation, leaving old guard history on a
    replica-only card. A same-second clear must outrank the guard it retired.
    """
    if isinstance(events, EventHistory):
        latest = events.ready_latest
    else:
        evs = [ev for ev in (events or []) if _event_kind(ev) != "dispatch_skipped"]  # skip records end no guard
        latest = max(evs, key=lambda ev: (_event_ts(ev), _task_field(ev, "id", 0) or 0)) if evs else None
    if latest is not None and _event_kind(latest) == "respawn_guarded":
        return "respawn_guarded"
    if _foreign_machines(task, cfg):
        return "foreign_runs_on"
    now = time.time() if now is None else now
    if _pinned_quota_hold_until(events, now):
        return "pinned_quota_wall"  # a fresh pin held on its live quota wall (kanban-dd62aef001)
    if _llm_paced_hold(events, now):
        return "llm_paced"  # the quota-pace governor holds it until its provider pool is back on pace
    if _episode_skip(events).get("reason") == "start_gate":
        return "start_gate"  # a plugin start gate (kanban_start_gate) held its last claim; asked again every tick
    return None


def _impossible_runs_on(task, cfg) -> Optional[list[str]]:
    """The card's runs_on when the Board registry is known and no enrolled machine carries every label (a typo or
    alias such as ``hermes-hub``), else None. The registry comes from cfg ``registry`` (``{me, machines}``) or, when
    absent, this process's cached registry read only (never a network fetch from a diagnostic)."""
    from hermes_cli import kanban_runs_on_capabilities as roc
    from hermes_cli import kanban_worker_node as wn
    labels = wn._parse(_task_field(task, "runs_on"))
    if not labels:
        return None
    reg = cfg["registry"] if "registry" in cfg else (wn._REGISTRY.get("value") if wn._REGISTRY.get("at") else None)
    if not reg:
        return None
    me = str(reg.get("me") or "")
    return labels if roc.impossible(labels, reg.get("machines") or {}, me) else None


def _rule_stranded_in_ready(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Assigned, unclaimed, ``ready`` for >= cfg["stranded_threshold_seconds"]
    (default 30 min). Deliberately age-based and identity-agnostic so it
    catches typo'd assignees, deleted profiles, and down external worker
    pools alike without a registry to curate. Unassigned tasks are excluded —
    the dispatcher's ``skipped_unassigned`` already covers them."""
    threshold_seconds = float(cfg.get("stranded_threshold_seconds", 30 * 60))
    if _task_field(task, "status") != "ready":
        return []
    # A live claim means it's being worked on even without progress yet.
    if _task_field(task, "claim_lock"):
        return []
    assignee = _task_field(task, "assignee") or ""
    if not assignee.strip():
        return []
    # A deliberate hold is reported by _rule_held_in_ready, not as a stranded card.
    if _ready_hold_reason(task, events, cfg, now):
        return []

    # Most recent event that put the task into ready; with none (old task /
    # truncated events) fall back to created_at — over-flagging an ancient
    # task beats missing a stranded one.
    # A task that is ready and unclaimed NOW re-entered ready after its last
    # claim/park, so those events bound the wait from below too. Without them a
    # card cycling claim -> scheduled -> condition_met -> ready all day read as
    # "ready 18h with no worker" from its first promotion. [daveey]
    last_ready_ts = _last_ready_entry_ts(events, runs)
    if last_ready_ts == 0:
        last_ready_ts = int(_task_field(task, "created_at", default=0) or 0)
    if last_ready_ts == 0:
        return []

    age_seconds = now - last_ready_ts
    if age_seconds < threshold_seconds:
        return []

    age_str = f"{age_seconds / 3600:.1f}h" if age_seconds >= 3600 else f"{int(age_seconds / 60)}m"
    # Escalate with age: <2x threshold warning, 2x-6x error, >6x critical.
    if age_seconds >= threshold_seconds * 6:
        severity = "critical"
    elif age_seconds >= threshold_seconds * 2:
        severity = "error"
    else:
        severity = "warning"

    actions = [
        DiagnosticAction(kind="reassign", label="Reassign to a different worker",
                         payload={"current_assignee": assignee}),
        _cli_hint("Check dispatcher status", "hermes kanban diagnostics"),
    ]
    detail = (f"This task has been ready for {age_str} but nothing has claimed it. Common causes: "
              f"assignee {assignee!r} is misspelled, the profile was deleted, or the external worker "
              f"pool for this lane is down. Confirm the assignee is correct and that a worker is "
              f"actually polling for it.")
    title = f"Ready for {age_str} with no worker"
    impossible = _impossible_runs_on(task, cfg)
    held: dict = {}
    if impossible:
        # [daveey] self-heal agent-7489fcf5cd: a stored label no enrolled machine carries never leases; any
        # worker may re-route it (kanban_creator_repin.stranded_refusal), so name that instead of a human edit.
        tid = _task_field(task, "id") or "<task_id>"
        held = {"held_reason": "impossible_runs_on", "runs_on": impossible}
        title = f"Ready for {age_str}: runs_on {impossible} matches no enrolled machine"
        detail = (f"No enrolled Board machine carries every label in runs_on={impossible} (labels_mismatch), so "
                  f"this card can never be claimed. Any worker can re-route it: kanban_move(task_id='{tid}', "
                  "runs_on=['machine:<registered-name>' or a real label], reason=...). This is agent work, not "
                  "an ask for David.")
        actions.insert(0, _cli_hint("Re-route to a real machine",
                                    f"hermes kanban move {tid} --runs-on machine:<registered-name>", suggested=True))
    return [Diagnostic(
        kind="stranded_in_ready", severity=severity,
        title=title,
        detail=detail,
        actions=actions,
        first_seen_at=last_ready_ts, last_seen_at=last_ready_ts, count=1,
        data={"ready_since": last_ready_ts, "age_seconds": int(age_seconds),
              "assignee": assignee, "threshold_seconds": int(threshold_seconds), **held},
    )]


def _rule_held_in_ready(task, events, runs, now, cfg) -> list[Diagnostic]:
    """Unclaimed ``ready`` >= the stranded threshold, but held on purpose (respawn guard, or a
    ``runs_on`` pin to another machine). Kept apart from ``stranded_in_ready`` so idle sweeps
    do not nag about cards they cannot unstick. [daveey]"""
    if _task_field(task, "status") != "ready" or _task_field(task, "claim_lock"):
        return []
    if not (_task_field(task, "assignee") or "").strip():
        return []
    reason = _ready_hold_reason(task, events, cfg, now)
    if not reason:
        return []
    since = _last_ready_entry_ts(events, runs) or int(_task_field(task, "created_at", 0) or 0)
    age = now - since
    if not since or age < float(cfg.get("stranded_threshold_seconds", 30 * 60)):
        return []
    guard_reason, hold_until = _guard_clock(events, runs, cfg, since) if reason == "respawn_guarded" else (None, None)
    if reason == "pinned_quota_wall":
        hold_until = _pinned_quota_hold_until(events, now)
    paced = _llm_paced_hold(events, now) if reason == "llm_paced" else None
    if reason == "start_gate":
        guard_reason = str(_episode_skip(events).get("detail") or "") or None  # the plugin's word
    if paced:
        guard_reason = str(paced.get("detail") or "") or None
        hold_until = int(paced["until"]) if isinstance(paced.get("until"), (int, float)) else None
    hold = f"{reason}: {guard_reason}" if guard_reason else reason
    if hold_until:
        stamp = time.strftime("%H:%M:%SZ", time.gmtime(hold_until))
        hold += f", until {stamp}" if hold_until > now else f", expired {stamp}"
    return [Diagnostic(
        kind="held_in_ready", severity="warning",
        title=(f"Ready {int(age / 60)}m, last observed hold ({hold})"
               if reason in ("respawn_guarded", "llm_paced", "start_gate")
               else f"Ready {int(age / 60)}m, held ({hold})"),
        detail=("The last recorded dispatch decision was a guard hold; the current hold is not verified. "
                "A cleared blocker, capacity limit, or missing pickup can leave this historical event "
                "unchanged. Check the execution machine's current dispatcher verdict."
                if reason == "respawn_guarded" else
                "The LLM quota-pace governor (kanban.llm_pacing) held its claim: the provider's OAuth pool burns "
                "ahead of its sustainable rate (paced) or every account is past its reserve (held), so the "
                "dispatcher keeps fewer runs on it. It starts on its own once the pool is back on pace; this "
                "is the last recorded verdict, not a live one." if reason == "llm_paced" else
                "Its explicit model pin is quota-walled and no same-class reroute applies; it starts when "
                "the wall resets." if reason == "pinned_quota_wall" else
                "A plugin start gate (kanban_start_gate hook, e.g. the token throttle while the fleet is over its "
                "cap) held its last claim. The dispatcher asks again every tick and starts it when the plugin stops "
                "holding; this is the last recorded verdict, not a live one." if reason == "start_gate" else
                "A foreign machine:* pin waits for that machine (or a re-file)."),
        actions=[_cli_hint("Check dispatcher status", "hermes kanban diagnostics")],
        first_seen_at=since, last_seen_at=since, count=1,
        data={"ready_since": since, "age_seconds": int(age), "hold_reason": reason,
              "foreign_machines": _foreign_machines(task, cfg),
              **({"guard_reason": guard_reason} if guard_reason else {}),
              **({"hold_until": hold_until} if hold_until else {})},
    )]


def _rule_overdue_wake(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A ``scheduled`` card parked by a timed ``kanban_continue`` wait is still parked more than
    cfg["overdue_wake_grace_seconds"] (default 10 min) after its ``wake_at``: the dispatcher's
    wake pass is not reaching it (dispatcher down, or a wake bug). [daveey]"""
    from hermes_cli import kanban_continuation as kc

    if _task_field(task, "status") != "scheduled":
        return []
    grace = float(cfg.get("overdue_wake_grace_seconds", kc.OVERDUE_WAKE_GRACE_SECONDS))
    parking = _latest_parking_event(events)
    if parking is None:
        return []
    payload = kc.pending_wake_payload(_event_kind(parking), _parse_payload(parking))
    if payload is None:
        return []
    wake_at = int(payload["wake_at"])
    late = now - wake_at
    if late <= grace:
        return []
    late_str = f"{late / 3600:.1f}h" if late >= 3600 else f"{int(late / 60)}m"
    return [Diagnostic(
        kind="overdue_wake", severity="critical" if late >= 3600 else "error",
        title=f"Scheduled wake is {late_str} overdue",
        detail=f"This task handed off a wait with kanban_continue and should have woken {late_str} "
               f"ago, but it is still scheduled. Check that the dispatcher is running; unblock the "
               f"task to resume it now.",
        actions=[DiagnosticAction(kind="unblock", label="Resume the task now", suggested=True),
                 _cli_hint("Check dispatcher status", "hermes kanban diagnostics")],
        first_seen_at=wake_at, last_seen_at=wake_at, count=1,
        data={"wake_at": wake_at, "late_seconds": int(late)},
    )]


def _latest_parking_event(events):
    from hermes_cli import kanban_continuation as kc

    if isinstance(events, EventHistory):
        return events.latest_parking
    parking = None
    for idx, ev in enumerate(events or []):
        if _event_kind(ev) not in kc.SCHEDULED_ENTRY_EVENTS:
            continue
        key = (_event_ts(ev), int(_task_field(ev, "id", default=0) or 0), idx)
        if parking is None or key >= parking[0]:
            parking = (key, ev)
    return parking[1] if parking else None


def _rule_scheduled_without_wake(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A ``scheduled`` card whose parking event carries no ``wake_at``: nothing on the board
    will ever wake it (parked by older code, or its waking cron was deleted). The dispatcher
    wakes it at the implied time (``kanban_timed_park.implied_wake``) or blocks a human-kind
    one; this makes the gap visible meanwhile. [daveey]"""
    from hermes_cli import kanban_continuation as kc
    from hermes_cli.kanban_timed_park import implied_wake

    if _task_field(task, "status") != "scheduled":
        return []
    ev = _latest_parking_event(events)
    payload = _parse_payload(ev) if ev is not None else {}
    if ev is not None and kc.pending_wake_payload(_event_kind(ev), payload) is not None:
        return []
    parked_at = _event_ts(ev) if ev is not None else int(_task_field(task, "created_at", default=now) or now)
    wake_at = implied_wake(payload, parked_at)
    human = _human_request(task) is not None
    return [Diagnostic(
        kind="scheduled_without_wake", severity="error" if human or wake_at <= now else "warning",
        title="Scheduled with no wake time",
        detail=("This card is parked in scheduled but its park recorded no wake time. "
                + ("It has an explicit human request; resolve that request before resuming."
                   if human else "This is an operational scheduling issue; existing execution guards still apply.")),
        actions=[_cli_hint("Inspect the parked task", f"hermes kanban show {_task_field(task, 'id')}",
                           suggested=True)],
        first_seen_at=parked_at, last_seen_at=parked_at, count=1,
        data={"parked_at": parked_at, "implied_wake_at": wake_at},
    )]


def _rule_clock_only_park(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A ``scheduled`` card waiting more than 2 h on a clock with no ``wake_check`` and no
    ``clock_only``: parked before condition wakes existed, so a condition that came true
    early sits unnoticed until the clock (t_9be6b361). [daveey]"""
    from hermes_cli import kanban_continuation as kc
    from hermes_cli.kanban_wake_check import GUARD_SECONDS

    if _task_field(task, "status") != "scheduled" or cfg.get("_wake_check"):
        return []
    ev = _latest_parking_event(events)
    if ev is None:
        return []
    payload = _parse_payload(ev)
    if kc.pending_wake_payload(_event_kind(ev), payload) is None:
        return []
    if payload.get("wake_check") or payload.get("clock_only"):
        return []
    wake_at = int(payload["wake_at"])
    if wake_at - now <= GUARD_SECONDS:
        return []
    task_id = str(_task_field(task, "id") or "")
    left = (wake_at - now) / 3600
    return [Diagnostic(
        kind="clock_only_park", severity="warning",
        title=f"Parked {left:.1f}h behind a clock with no wake check",
        detail=("This card waits on a clock with no wake_check probe and was not marked "
                "clock_only. If it waits on a readable fact (a DB row, a tag, a file, a log line), "
                "re-park it with a wake_check so it wakes when the fact turns true; if it waits on "
                "another card, link it as a parent instead."),
        actions=[_cli_hint("Re-park with a probe",
                           f"hermes kanban schedule {task_id} --wake-check '<cmd: exit 0 when ready>'",
                           suggested=True)],
        first_seen_at=_event_ts(ev), last_seen_at=_event_ts(ev), count=1,
        data={"wake_at": wake_at},
    )]


def _rule_wake_check_health(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A parked card's ``wake_check`` that is failing (not exit 0/1), or has not run long after
    it was due (dispatcher down or probe pool wedged). [daveey]"""
    state = cfg.get("_wake_check")
    if not state or _task_field(task, "status") != "scheduled":
        return []
    task_id = str(_task_field(task, "id") or "")
    out: list[Diagnostic] = []
    fails = int(state.get("fails") or 0)
    if fails:
        out.append(Diagnostic(
            kind="wake_check_failing", severity="warning",
            title=f"Wake check failing ({fails} in a row, rc {state.get('last_rc')})",
            detail=(f"The probe `{state.get('cmd')}` exited with neither 0 nor 1. After 3 failures "
                    f"in a row the card wakes so a worker can fix it. Last output: "
                    f"{(state.get('last_out') or '(none)')[-200:]}"),
            actions=[_cli_hint("Inspect the card", f"hermes kanban show {task_id}", suggested=True)],
            first_seen_at=state.get("last_at") or now, last_seen_at=state.get("last_at") or now,
            count=fails, data={"cmd": state.get("cmd"), "last_rc": state.get("last_rc")},
        ))
    late = now - int(state.get("next_at") or now)
    grace = max(600, int(state.get("every") or 0))
    if late > grace:
        out.append(Diagnostic(
            kind="wake_check_overdue", severity="error",
            title=f"Wake check {late // 60}m overdue",
            detail=("The probe was due but has not run. Check that the dispatcher is running "
                    "(`hermes kanban diagnostics`), or resume the task now."),
            actions=[DiagnosticAction(kind="unblock", label="Resume the task now", suggested=True)],
            first_seen_at=int(state["next_at"]), last_seen_at=int(state["next_at"]), count=1,
            data={"next_at": state.get("next_at"), "late_seconds": late},
        ))
    return out


def _block_loop_ask(events) -> Optional[str]:
    """Reason of the newest ``block_loop_detected`` event with no human input after it."""
    latest = None
    for ev in events or []:
        kind = _event_kind(ev)
        if kind == "block_loop_detected":
            latest = ev
        elif latest is not None and kind in ("commented", "edited", "status", "unblocked", "promoted"):
            latest = None
    if latest is None:
        return None
    return str(_parse_payload(latest).get("reason") or "").strip() or "(no reason recorded)"


def _rule_escalated_waiting_on_user(task, events, runs, now, cfg) -> list[Diagnostic]:
    """An escalated triage card needs the user only with explicit current evidence."""
    request = _human_request(task)
    if _task_field(task, "status") != "triage" or request is None:
        return []
    if _block_loop_ask(events) is None:
        return []
    ask = request["action"]
    ts = _latest_event_ts(events, {"block_loop_detected"}) or now
    task_id = str(_task_field(task, "id") or "")
    return [Diagnostic(
        kind="escalated_waiting_on_user", severity="error",
        title="Waiting on you (block-loop escalation)",
        detail=f"The worker re-blocked on the same ask repeatedly, so the card was escalated to triage: {ask}",
        actions=[_cli_hint("Answer, then return it to work", f"hermes kanban promote {task_id}", suggested=True)],
        first_seen_at=ts, last_seen_at=ts, count=1, data={"ask": ask},
    )]


def _rule_waiting_behind_user(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A todo card gated by a parent's explicit human request."""
    if _task_field(task, "status") != "todo":
        return []
    graph = cfg.get("_graph")
    if not isinstance(graph, dict):
        return []
    waiting = [p for p in (graph.get("parents") or []) if isinstance(p, dict)
               and p.get("status") in ("blocked", "triage") and _human_request(p)]
    if not waiting:
        return []
    ids = [str(p.get("id")) for p in waiting]
    return [Diagnostic(
        kind="waiting_behind_user", severity="warning",
        title=f"Waiting behind {len(ids)} parent(s) that need you",
        detail="This card cannot start until these parents get a human answer: " + ", ".join(ids),
        actions=[_cli_hint("Show the blocking parent", f"hermes kanban show {ids[0]}", suggested=True)],
        first_seen_at=now, last_seen_at=now, count=len(ids), data={"waiting_on": ids},
    )]


def _rule_stale_running(task, events, runs, now, cfg) -> list[Diagnostic]:
    """A ``running`` card whose claim expired >= cfg["stale_running_grace_seconds"]
    (default 1h) ago with no live recorded worker.

    Nothing in the dispatcher is going to free this card — its reclaim paths can decline a
    card whose owner receipt never resolved, and until they could not, the failure was
    invisible: an operator saw only the cards queued behind the profile's worker cap. So the
    jam itself is the signal. Silent in every shape that can still progress: a live claim
    (the owner is still working), a heartbeat inside the dispatcher's max-stale window, a
    foreign host's lock (its pids say nothing here), or a live recorded worker. Read-only.
    """
    grace_seconds = float(cfg.get("stale_running_grace_seconds", 60 * 60))
    if _task_field(task, "status") != "running":
        return []
    from hermes_cli import kanban_db as kb
    expires = kb._to_epoch(_task_field(task, "claim_expires", None))
    if expires is None or (now - expires) < grace_seconds:
        return []
    lock = str(_task_field(task, "claim_lock", "") or "")
    if not lock.startswith(kb._host_prefix()):
        return []
    hb = kb._to_epoch(_task_field(task, "last_heartbeat_at", None))
    if hb is not None and (now - hb) <= kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS:
        return []

    from hermes_cli.kanban_db_dispatch import _worker_alive
    recorded = [(_task_field(task, "worker_pid", None),
                 _task_field(task, "worker_started_at", None))]
    # Only this attempt's pids speak for it: an earlier run's worker that is still alive
    # (or whose number was reused) says nothing about whether the current run is executing.
    run_id = _task_field(task, "current_run_id", None)
    for ev in events:
        if run_id is not None and _task_field(ev, "run_id", None) != run_id:
            continue
        if _event_kind(ev) in {"spawned", "worker_bound"}:
            payload = _parse_payload(ev)
            recorded.append((payload.get("pid"), payload.get("started_at")))
    pids = sorted({int(pid) for pid, _ in recorded if pid})
    if any(_worker_alive(int(pid), started_at) for pid, started_at in recorded if pid):
        return []

    expired_seconds = now - expires
    age_str = (f"{expired_seconds / 3600:.1f}h" if expired_seconds >= 3600
               else f"{int(expired_seconds / 60)}m")
    # Escalate with age: <2x grace warning, 2x-6x error, >6x critical.
    if expired_seconds >= grace_seconds * 6:
        severity = "critical"
    elif expired_seconds >= grace_seconds * 2:
        severity = "error"
    else:
        severity = "warning"

    task_id = str(_task_field(task, "id") or "")
    return [Diagnostic(
        kind="stale_running", severity=severity,
        title=f"Running {age_str} past its expired claim with no live worker",
        detail=f"Nothing is executing this card and nothing has reclaimed it: the claim expired "
               f"{age_str} ago, no heartbeat arrived inside the last hour, and no pid recorded for "
               f"the run ({', '.join(str(p) for p in pids) or 'none'}) is alive. The dispatcher "
               f"declines a reclaim when the run's owner receipt never resolved, so the card can "
               f"hold its profile's worker slot indefinitely while every card queued behind it "
               f"stays deferred. Check the card's log, then reclaim it.",
        actions=[DiagnosticAction(kind="reclaim", label="Reclaim the dead claim", suggested=True),
                 _cli_hint("Reclaim from the CLI", f"hermes kanban reclaim {task_id}")],
        first_seen_at=expires, last_seen_at=expires, count=1,
        data={"claim_expires": expires, "expired_seconds": int(expired_seconds),
              "recorded_pids": pids, "last_heartbeat_at": hb,
              "grace_seconds": int(grace_seconds)},
    )]


# Order matters: earlier rules render first on severity ties.
_RULES: list[RuleFn] = [
    _rule_hallucinated_cards,
    _rule_triage_aux_unavailable,
    _rule_prose_phantom_refs,
    _rule_repeated_failures,
    _rule_repeated_crashes,
    _rule_review_dependency_deadlock,
    _rule_running_with_open_parents,
    _rule_stuck_in_blocked,
    _rule_block_unblock_cycling,
    _rule_stranded_in_ready,
    _rule_held_in_ready,
    _rule_overdue_wake,
    _rule_scheduled_without_wake,
    _rule_clock_only_park,
    _rule_wake_check_health,
    _rule_escalated_waiting_on_user,
    _rule_waiting_behind_user,
    _rule_stale_running,
]


DEFAULT_CONFIG = {
    # Match the dispatcher default (kanban.failure_limit) so repeated-failure
    # diagnostics do not lag behind the default auto-block threshold.
    "failure_threshold": 2,
    # Legacy alias accepted at read time by _rule_repeated_failures.
    "spawn_failure_threshold": 2,
    "crash_threshold": 2,
    "blocked_stale_hours": 24,
    # Below 30 min the signal is dominated by tasks about to be claimed on
    # the next dispatcher tick.
    "stranded_threshold_seconds": 30 * 60,
    # The dispatcher reclaims an expired claim on its own next tick, so a card
    # still `running` an hour past its expiry means every reclaim path declined
    # it. Same hour the heartbeat backstop uses.
    "stale_running_grace_seconds": 60 * 60,
}


def _has_explicit_threshold(cfg: dict) -> bool:
    return "failure_threshold" in cfg or "spawn_failure_threshold" in cfg


def config_from_kanban_config(kanban_cfg: Optional[dict]) -> dict:
    """Diagnostics config from the ``kanban`` section. ``kanban.diagnostics.
    failure_threshold`` is an explicit override; otherwise the threshold is
    ``kanban.failure_limit`` so diagnostics match the dispatcher's breaker."""
    kanban_cfg = kanban_cfg or {}
    diag_cfg = dict(kanban_cfg.get("diagnostics") or {})
    diag_cfg.setdefault(
        "failure_limit", kanban_cfg.get("failure_limit", DEFAULT_CONFIG["failure_threshold"]),
    )
    if not _has_explicit_threshold(diag_cfg):
        diag_cfg["failure_threshold"] = diag_cfg["failure_limit"]
    return diag_cfg


def config_from_runtime_config(raw_config: Optional[dict]) -> dict:
    """Diagnostics config from the full runtime config: folds ``kanban`` through
    ``config_from_kanban_config`` and carries ``kanban``/``auxiliary``/``model``
    through for the triage-aware rules."""
    raw_config = raw_config or {}
    if not isinstance(raw_config, dict):
        return {}
    cfg: dict = {}
    kanban_cfg = raw_config.get("kanban")
    if isinstance(kanban_cfg, dict):
        cfg.update(config_from_kanban_config(kanban_cfg))
        cfg["kanban"] = kanban_cfg
    for key in ("auxiliary", "model"):
        value = raw_config.get(key)
        if value is not None:
            cfg[key] = value
    return cfg


def compute_task_diagnostics(
    task,
    events: list | tuple,
    runs: list,
    *,
    now: Optional[int] = None,
    config: Optional[dict] = None,
    graph: Optional[dict] = None,
    wake_check: Optional[dict] = None,
) -> list[Diagnostic]:
    """Run every rule for one task; critical first, then error, warning; ties
    broken by most-recent ``last_seen_at``."""
    now_ts = int(now if now is not None else time.time())
    config = config or {}
    cfg = {**DEFAULT_CONFIG, **config}
    if graph is not None:
        cfg["_graph"] = graph
    cfg["_wake_check"] = wake_check
    if not _has_explicit_threshold(config) and "failure_limit" in config:
        cfg["failure_threshold"] = _positive_int(
            config.get("failure_limit"), DEFAULT_CONFIG["failure_threshold"],
        )
    out: list[Diagnostic] = []
    for rule in _RULES:
        try:
            out.extend(rule(task, events, runs, now_ts, cfg))
        except Exception:
            # A broken rule must never 500 a whole /board request.
            continue
    severity_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    out.sort(key=lambda d: (-severity_idx.get(d.severity, -1), -(d.last_seen_at or 0)))
    return out
