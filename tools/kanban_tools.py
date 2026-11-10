"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

Registered only under the dispatcher (``HERMES_KANBAN_TASK`` set) or when the profile
enables the ``kanban`` toolset. Tools rather than ``hermes kanban`` shell-outs: they run
in the agent's process (reach ``kanban.db`` from a container/SSH terminal backend, no
shlex quoting of JSON metadata, structured-JSON failures). Humans use CLI/dashboard.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import time
from tools.kanban_tools_board import tool_board as _board
from typing import Any, Callable, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import handoff_judge_goal, judge_goal
from tools.registry import no_cache_check_fn, registry, tool_error
from hermes_cli.config import cfg_get, load_config
from hermes_cli.kanban_capabilities import check_tool_create, declaration_required
from hermes_cli import kanban_contract_cli as _contract_cli, kanban_overlap, kanban_reason_comment as krc
from tools.kanban_tools_placement import (_local_placement, _machine_placement, _marker_placement_fields, _persisted_session_id,
                                          _create_session_id, _placement_origin, _placement_refused, _runs_where,
                                          _effective_board_args, _origin_enrollment)
from tools.kanban_tools_db_hint import generic_tool_error
from tools.kanban_tools_park_args import (_blocker_id_arg, _clear_blocker_arg, _blocker_scope_args, _park_guard,
                                          _wake_check_args, _wake_check_echo)
from hermes_cli.kanban_ask_resolved import ask_checks, create_ask_check, filing_context
from tools import kanban_tools_completion as _kc, kanban_tools_create_args as _ca
from tools.kanban_tools_schemas import (
    KANBAN_ATTACH_SCHEMA,
    KANBAN_ATTACH_URL_SCHEMA, KANBAN_ATTACHMENTS_SCHEMA, KANBAN_BLOCK_SCHEMA, KANBAN_COMMENT_SCHEMA,
    KANBAN_COMPLETE_SCHEMA, KANBAN_CONTINUE_SCHEMA, KANBAN_MOVE_SCHEMA, KANBAN_CREATE_SCHEMA, KANBAN_HEARTBEAT_SCHEMA, KANBAN_LINK_SCHEMA,
    KANBAN_LIST_SCHEMA, KANBAN_ORIGIN_ADOPT_SCHEMA, KANBAN_REQUEST_CHANGES_SCHEMA, KANBAN_REQUEST_REVIEW_SCHEMA,
    KANBAN_SCHEDULE_SCHEMA, KANBAN_SHOW_SCHEMA, KANBAN_UNBLOCK_SCHEMA, KANBAN_ARCHIVE_SCHEMA,
    KANBAN_SUPERVISOR_REQUEST_SCHEMA, KANBAN_UNLINK_SCHEMA)

logger = logging.getLogger(__name__)

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


# --- Gating ---

def _profile_has_kanban_toolset() -> bool:
    from tools.kanban_toolset_context import kanban_toolset_requested

    requested = kanban_toolset_requested()
    if requested:
        return True
    try:
        config = load_config()
        # Preserve the legacy profile-wide opt-in for callers using bundles.
        if "kanban" in (config.get("toolsets") or []):
            return True
        if requested is not None:
            # Never borrow another platform's opt-in during schema assembly.
            return False
        # Offer-time skill discovery has no platform selection. A saved opt-in
        # makes the playbook relevant; actual schemas still use the scope above.
        from hermes_cli.tools_config import _get_platform_tools

        platforms = config.get("platform_toolsets") or {}
        return any(
            "kanban" in _get_platform_tools(config, platform, include_default_mcp_servers=False)
            for platform, names in platforms.items() if isinstance(names, list)
        )
    except Exception:
        return False


def _delegation_ctx(predicate: str, default: bool) -> bool:
    """``agent.delegation_context.<predicate>()``; ``default`` when it cannot be evaluated."""
    try:
        from agent import delegation_context
        return getattr(delegation_context, predicate)()
    except Exception:
        return default


def _is_delegated_child_context() -> bool:
    return _delegation_ctx("is_delegated_child_context", False)


def _is_dispatcher_owned_worker() -> bool:
    """False for delegate_task children AND for cron jobs fired in-process from
    a worker — i.e. whenever HERMES_KANBAN_* is present but not ours."""
    return _delegation_ctx("is_dispatcher_owned_worker_context", True)


def _visible(*, to_env_worker: bool) -> bool:
    """check_fn core: never for delegate children; dispatcher-spawned env workers
    (HERMES_KANBAN_TASK) per flag; else the profile toolset decides."""
    if _is_delegated_child_context():
        return False
    if os.environ.get("HERMES_KANBAN_TASK") and _is_dispatcher_owned_worker():
        return to_env_worker
    # [daveey] A session started for a chat on another gateway (``hermes chat --origin-session``) exists to
    # create that chat's cards: it always gets the real tools, never a private-handler fallback.
    from hermes_cli.kanban_origin import ambient_origin_ref
    if ambient_origin_ref():
        return True
    return _profile_has_kanban_toolset()


@no_cache_check_fn
def _check_kanban_mode() -> bool:
    """Lifecycle tools: dispatcher workers + profiles with the ``kanban`` toolset."""
    return _visible(to_env_worker=True)


@no_cache_check_fn
def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list): hidden from task workers."""
    return _visible(to_env_worker=False)


# --- Shared helpers: validation failures raise _Reject; _kanban_handler renders it ---

# Worker tools that terminate or transition a run's ownership. An unbound worker
# (HERMES_KANBAN_RUN_ID unresolvable) must not run these: expected_run_id=None
# would silently skip the run-ownership CAS in kanban_db. Non-lifecycle tools
# (heartbeat / attach / attach_url) do not terminate a run and are not gated.
_RUN_LIFECYCLE_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_schedule", "kanban_continue", "kanban_move",
    "kanban_request_review", "kanban_request_changes",
})

class _Reject(Exception):
    """Carries a finished ``tool_error`` payload out of a validation helper.

    ``rejection`` stamps a stable machine-readable class on the payload (see
    ``COMPLETE_REJECTION_CLASSES``) so digests can count refusals by cause
    instead of grepping prose that changes between releases."""

    def __init__(self, message: str, rejection: Optional[str] = None, *, payload: Optional[str] = None):
        extra = {"rejection": rejection} if rejection else {}
        super().__init__(payload or tool_error(message, **extra))
        self.rejection = rejection


def _check(cond: Any, message: str, rejection: Optional[str] = None) -> None:
    """Reject (as a tool error) unless ``cond`` is truthy."""
    if not cond:
        raise _Reject(message, rejection)


# Stable ``rejection`` classes on every kanban_complete tool_error. Consumers
# (coach digests, board analytics) key on these strings: add, never rename.
COMPLETE_REJECTION_CLASSES = frozenset({
    "goal_judge",     # goal-mode completion judge said not done / blocked
    "phantom_cards",  # created_cards names ids this worker did not create
    "artifacts",      # declared artifacts could not be preserved
    "empty",          # no summary/result, or an evidence-free completion
    "live_claim",     # another live dispatcher worker owns the card
    "stale_run",      # run superseded, card already terminal, or unknown id
    "not_running",    # card waits (scheduled/todo/ready) with no run: archive it or re-park it as a watch [daveey]
    "foreign_card",   # card exists here only as a Board replica copy; its owner gateway must write it [daveey]
    "pr_acceptance",  # completion contract's PR CI acceptance refused (pr_acceptance event ok=false)
    "parents",        # unsatisfied parent dependencies
    "not_owner",      # delegate child / other task / unbound run id
    "invalid_args",   # unknown parameter or malformed argument
    "error",          # unexpected exception (logged)
})


def _tag_rejection(payload: str, rejection: str) -> str:
    """Add ``rejection`` to a tool_error JSON payload that lacks one."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return payload
    if not isinstance(data, dict) or "error" not in data or data.get("rejection"):
        return payload
    data["rejection"] = rejection
    return json.dumps(data, ensure_ascii=False)


# Keys a handler reads that its LLM-facing schema deliberately does not declare:
# ``session_id`` is provenance stamped by internal callers (31fe2290393), ``project_id``
# the pre-``project`` alias still honoured by ``_handle_create`` (e7811345c17).
_UNDECLARED_ARGS: dict[str, frozenset[str]] = {
    "kanban_create": frozenset({"session_id", "project_id"}),
    # ``title`` is the pre-schema alias of ``filename`` that ``_handle_attach_url`` still honours.
    "kanban_attach_url": frozenset({"title"}),
}


def _persisted_identity() -> str:
    """Profile name persisted into board records (comment author, task creator).

    ``hermes_cli.profiles.current_profile_name`` resolves the profile this call runs FOR — the bound
    home override under a multiplexed tick or turn, else the dispatcher's ``HERMES_PROFILE`` pin,
    else the process home; the generic ``"worker"`` only when nothing names a profile. Never taken
    from tool args: board records are injected into future workers' prompts, so a caller-supplied
    identity could forge an authoritative-looking author (see #19713).
    """
    from hermes_cli.profiles import current_profile_name

    return current_profile_name("worker") or "worker"


def _kanban_handler(tool_name: str, *, classify_rejections: bool = False) -> Callable:
    """Wrap a handler so every failure is a structured tool error. ``ValueError``
    (invalid board slug, DB validation such as cycle/self-link, ``AttachmentTooLarge``)
    is reported without a traceback; anything else is logged with ``logger.exception``.
    With ``classify_rejections`` every error carries a ``rejection`` class: an
    untagged ``_Reject`` is ``invalid_args``, any other exception ``error``."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(args: dict, **kw) -> str:
            try:
                # Reject typos before a handoff can succeed without its artifacts.
                properties = registry.get_schema(tool_name)["parameters"]["properties"]
                allowed = set(properties) | _UNDECLARED_ARGS.get(tool_name, frozenset())
                unknown = sorted(set(args) - allowed)
                _check(not unknown,
                       f"{tool_name}: unknown parameter(s): {', '.join(unknown)}. "
                       f"Valid parameters: {', '.join(sorted(properties))}. Nothing changed.",
                       "invalid_args")
                refused = _remote_lease_refusal(tool_name, args)
                if refused is not None:
                    return refused
                out = fn(args, **kw)
                if tool_name in _RUN_LIFECYCLE_TOOLS and isinstance(out, str) and out.startswith('{"ok": true'):
                    from tools import kanban_tools_run_end as kre
                    out = kre.annotate_success(_board, args, out)
                if classify_rejections and isinstance(out, str) and out.startswith('{"error"'):
                    out = _tag_rejection(out, "invalid_args")
                return out
            except _Reject as e:
                if classify_rejections:
                    return _tag_rejection(e.args[0], e.rejection or "invalid_args")
                return e.args[0]
            except Exception as e:
                if tool_name in {"kanban_create", "kanban_origin_adopt"} and kw.get("origin_intent") is not None:
                    from hermes_cli.kanban_origin import OriginError
                    if isinstance(e, OriginError):
                        return tool_error(str(e), code=e.code, handoff_complete=False)

                from hermes_cli.kanban_capabilities import CapabilityMismatchError
                from hermes_cli.kanban_assignment_routing import AssignmentRoutingError
                from hermes_cli.kanban_model_assignment import ModelProviderMismatchError
                from hermes_cli.kanban_awaiting import AwaitingError
                from hermes_cli.kanban_pin_provider import PinProviderUnresolvedError
                if isinstance(e, (AssignmentRoutingError, ModelProviderMismatchError, AwaitingError,
                                  PinProviderUnresolvedError)):
                    return tool_error(str(e), code=e.code)
                if isinstance(e, CapabilityMismatchError):
                    return tool_error(str(e), code=e.code, assignee=e.assignee, missing=e.missing)
                if isinstance(getattr(e, "tool_error_fields", None), dict):  # e.g. RequiresDeclarationMissing [daveey]
                    return tool_error(str(e), code=e.code, **e.tool_error_fields)
                from hermes_cli.kanban_skill_check import UnknownSkillError
                if isinstance(e, UnknownSkillError):  # the worker could never load it: refuse, don't create [daveey]
                    return tool_error(str(e), code=e.code, rejection="invalid_args", assignee=e.assignee,
                                      unknown_skills=e.missing, suggestions=e.suggestions)

                if not isinstance(e, ValueError):
                    logger.exception(f"{tool_name} failed")
                return generic_tool_error(tool_name, args, e, classify_rejections)
        return wrapper
    return deco


# Tools whose write moves a card's lifecycle columns, which the Board fences to the lease holder (``live_claim``).
# kanban_archive is not here: it sends the holder a stop request instead (kanban_remote_stop). [daveey]
_LEASE_FENCED_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_unblock", "kanban_schedule", "kanban_continue",
    "kanban_request_review", "kanban_request_changes",
})


def _remote_lease_refusal(tool_name: str, args: dict) -> Optional[str]:
    """A typed refusal when the target card runs under another machine's live Board lease, else None.

    Such a write would be committed here and then rejected by the Board (``live_claim``) after the tool had
    reported success: the card reads back as before and the caller never learns. Refusing up front says so."""
    if tool_name not in _LEASE_FENCED_TOOLS:
        return None
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return None
    try:
        from hermes_cli.kanban_remote_stop import remote_holder
        with _board(args.get("board"), quiet_close=True) as (_kb, conn):
            holder = remote_holder(conn, str(tid))
    except Exception:
        logger.debug("%s: remote lease check for %s failed", tool_name, tid, exc_info=True)
        return None  # the handler reports its own board errors
    if holder is None:
        return None
    return tool_error(
        f"{tool_name} refused: {tid} is running under {holder}'s live lease on the Board, which accepts "
        f"lifecycle writes only from the lease holder. Nothing changed. Act on {holder}, use kanban_archive "
        f"(it sends {holder} a stop request), or wait for the run to end.",
        rejection="live_claim", code="remote_live_lease", holder=holder)


# The delegate-child / worker-subprocess write fence lives in a sibling (line cap); re-exported here. [daveey]
from tools.kanban_tools_fence import _env_fence_covers_board, _reject_delegated_child_mutation


def _default_task_id(arg: Any) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg is not None:
        val = str(arg).strip()
        if val:
            return val
    if _is_delegated_child_context():
        return None
    if not _is_dispatcher_owned_worker():
        # A cron job fired in-process from a worker must never inherit the
        # worker's task id as an implicit default.
        return None
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid:
        val = env_tid.strip()
        if val:
            return val
    return None


def _require_task_id(args: dict) -> str:
    """Resolve the target task or reject with a message the caller can act on.

    The env default only exists for a dispatcher-spawned worker; every other
    caller must name a task, so a rejection points at ``kanban_list`` instead of
    an env var a chat caller cannot set (#91431).
    """
    tid = _default_task_id(args.get("task_id"))
    if tid:
        return tid
    if os.environ.get("HERMES_KANBAN_TASK"):
        # Env task present but not usable here (delegate child / cron run beside
        # a worker): it is not this session's to default to.
        raise _Reject(
            "task_id is required: this session does not own the inherited "
            "HERMES_KANBAN_TASK, so it is not a valid default. Pass an explicit "
            "task_id.")
    raise _Reject(
        "task_id is required: this session has no dispatcher-assigned task to "
        "default to. Pass an explicit task_id; discover task ids with kanban_list.")


def _own_task_env(task_id: str, var: str) -> Optional[str]:
    """``$var`` only when this worker is scoped to ``task_id``; else None."""
    return os.environ.get(var) if os.environ.get("HERMES_KANBAN_TASK") == task_id else None

def _worker_run_id(task_id: str) -> Optional[int]:
    """This worker's dispatcher run id when it is scoped to task_id."""
    raw = _own_task_env(task_id, "HERMES_KANBAN_RUN_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _stamp_worker_session_metadata(task_id: str, metadata: Optional[dict]) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    session_id = _own_task_env(task_id, "HERMES_SESSION_ID")
    return {**(metadata or {}), "worker_session_id": session_id} if session_id else metadata


def _enforce_worker_task_ownership(tid: str) -> None:
    """A dispatcher-spawned worker may only mutate its own HERMES_KANBAN_TASK; a
    prompt-injected ``task_id`` must not corrupt sibling/cross-tenant runs.
    Orchestrators (toolset enabled, no env task) legitimately route child tasks.

    Tools like ``kanban_complete`` / ``kanban_block`` / ``kanban_heartbeat`` mutate run-lifecycle state, so
    a buggy or prompt-injected worker that passed an explicit ``task_id`` for some other task could corrupt
    sibling or cross-tenant runs (see #19534).
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and tid != env_tid:
        raise _Reject(
            f"worker is scoped to task {env_tid}; refusing to mutate {tid}. Use kanban_comment "
            f"to hand off information to other tasks, or kanban_create to spawn follow-up work.")


def _worker_guard(tool_name: str, args: dict) -> str:
    """Worker mutation preamble, in order: delegate-child rejection, task id
    resolution, task-scope ownership, run-identity proof. Returns the task id.

    A dispatcher-spawned worker (``HERMES_KANBAN_TASK`` set) that cannot name
    its run id is refused on the run-lifecycle mutations: ``expected_run_id=None``
    would silently skip the run-ownership CAS in ``kanban_db`` (``complete_task`` /
    ``block_task`` / ``request_review`` / ``request_changes`` only append
    ``AND current_run_id = ?`` when the value is not ``None``), so an unbound
    stale worker could complete a card a live successor owns. This mirrors
    ``agent/kanban_stop.py``, which already treats an unbound run id as unknown
    and fails closed. CLI / human / orchestrator paths (no ``HERMES_KANBAN_TASK``)
    legitimately pass ``expected_run_id=None`` and are unaffected. Non-lifecycle
    worker tools (heartbeat / attach / attach_url) do not terminate a run and are
    not gated here.
    """
    _reject_delegated_child_mutation(tool_name, args)
    tid = _require_task_id(args)
    _enforce_worker_task_ownership(tid)
    if (
        tool_name in _RUN_LIFECYCLE_TOOLS
        and os.environ.get("HERMES_KANBAN_TASK")
        and _worker_run_id(tid) is None
    ):
        raise _Reject(
            f"{tool_name} refused: this worker cannot resolve its "
            "HERMES_KANBAN_RUN_ID, so it cannot prove ownership of the card's "
            "current run. A stale or unbound worker must not terminate a run a "
            "live successor owns. Re-run through the dispatcher so the run id is "
            "pinned, or use an orchestrator/CLI path that passes an explicit "
            "expected_run_id."
        )
    if tool_name in _RUN_LIFECYCLE_TOOLS - {"kanban_complete"}:  # complete keeps its own-park close
        from tools import kanban_tools_run_end as kre
        refused = kre.guard_refusal(_board, args, tool_name, tid)
        if refused:
            raise _Reject("", kre.CODE, payload=refused)
    return tid


def _require_orchestrator_tool(tool_name: str) -> None:
    """The check_fn already hides orchestrator tools from workers; this catches
    a stale registration or test harness routing a worker here anyway."""
    if os.environ.get("HERMES_KANBAN_TASK"):
        raise _Reject(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers must use "
            "kanban_complete, kanban_request_review, kanban_request_changes, kanban_block, "
            "kanban_heartbeat, or kanban_comment for their assigned task.")


def _existing_task(kb, conn, tid: str):
    task = kb.get_task(conn, tid)
    _check(task is not None, f"task {tid} not found")
    return task


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _ok_landed(kb, conn, tid: str, default_status: str, **extra: Any) -> str:
    """Success payload reporting where the task actually landed (routing may
    not leave it in the requested status)."""
    run = kb.latest_run(conn, tid)
    landed = kb.get_task(conn, tid)
    return _ok(task_id=tid, run_id=run.id if run else None,
               status=landed.status if landed else default_status, **extra)


def _escalated(kb, conn, tid: str) -> Optional[str]:
    """The result for a park that escalated to an owner card instead (``kanban_awaiting``). [daveey]"""
    from hermes_cli import kanban_awaiting as kaw

    owner = kaw.escalated_owner(conn, tid)
    if owner is None:
        return None
    return _ok_landed(kb, conn, tid, "todo", code="repark_escalated", owner_task_id=owner,
                      next=kaw.escalation_message(tid, owner))


def _redact(value: Any) -> str:
    return redact_sensitive_text(str(value), force=True)


def _redact_opt(value: Any) -> Any:
    return _redact(value) if value else value


def _redact_metadata(metadata: dict) -> Optional[dict]:
    """Redact via a JSON round-trip; None if the result can't be re-parsed."""
    try:
        return json.loads(redact_sensitive_text(json.dumps(metadata), force=True))
    except json.JSONDecodeError:
        return None


def _coerce_str_list(value: Any, name: str, what: str, *, strip: bool = False):
    """Accept a single string (convenience) or a list/tuple; with ``strip`` the
    items are stringified, stripped, and empties dropped."""
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _Reject(f"{name} must be a list of {what}, got {type(value).__name__}")
    if strip:
        value = [str(x).strip() for x in value if str(x).strip()]
    return value


def _require_dict_metadata(metadata: Any) -> None:
    _check(metadata is None or isinstance(metadata, dict),
           f"metadata must be an object/dict, got {type(metadata).__name__}")


def _merge_artifacts(metadata: Any, artifacts: list[str]) -> dict:
    """Fold ``artifacts`` into ``metadata["artifacts"]`` (merged with, never overwriting, a
    list the worker passed manually). Artifacts ride inside metadata so the completed-event
    payload needs no DB schema change; the gateway notifier uploads each as an attachment."""
    _require_dict_metadata(metadata)
    metadata = {} if metadata is None else metadata
    existing = metadata.get("artifacts")
    if isinstance(existing, (list, tuple)):
        merged = (str(item).strip() for item in [*existing, *artifacts])
        metadata["artifacts"] = list(dict.fromkeys(s for s in merged if s))
    else:
        metadata["artifacts"] = artifacts
    return metadata


def _require_text(args: dict, name: str, message: Optional[str] = None) -> Any:
    """``args[name]``; rejects when missing or blank."""
    value = args.get(name)
    _check(value and str(value).strip(), message or f"{name} is required")
    return value


_BOOL_WORDS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


def _parse_bool_arg(args: dict, name: str) -> bool:
    value = args.get(name)
    if value is None or isinstance(value, bool):
        return bool(value)
    parsed = _BOOL_WORDS.get(str(value).strip().lower())
    _check(parsed is not None, f"{name} must be a boolean or 'true'/'false'")
    return parsed


def _opt_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    return int(value) if value is not None else default


_TASK_FIELDS = tuple(
    "id title body assignee status tenant priority workspace_kind workspace_path created_by "
    "created_at started_at completed_at result current_run_id model_override "
    "provider_override completion_contract last_failure_error block_kind human_request".split())
_TASK_SUMMARY_FIELDS = tuple(
    "id title assignee status priority tenant workspace_kind workspace_path project_id created_by "
    "created_at started_at completed_at current_run_id model_override provider_override block_kind human_request".split())
_RUN_FIELDS = tuple("id profile status outcome summary error metadata started_at ended_at".split())
_COMMENT_FIELDS = ("author", "body", "created_at")
_EVENT_FIELDS = ("kind", "payload", "created_at", "run_id")
_ATTACHMENT_FIELDS = tuple(
    "id filename content_type size uploaded_by stored_path created_at".split())


def _fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """``{name: getattr(obj, name)}``; every value None when ``obj`` is None."""
    return {n: getattr(obj, n) if obj is not None else None for n in names}


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        **_fields(task, _TASK_SUMMARY_FIELDS), "parents": parents, "children": children,
        "parent_count": len(parents), "child_count": len(children)}


# --- Goal-mode judge gate ---

_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input", "time", "assignment"})


def _goal_judge_available() -> bool:
    """``judge_goal`` fails open (no auxiliary model -> ``"continue"``), which is
    indistinguishable from "not done yet" and would wedge every goal_mode
    worker; so the gate is enforced only when a judge is actually reachable."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


def _goal_gate(tool_name: str, task, tid: str, evidence: str, *, conn=None):
    """Goal-mode pre-handoff judge gate: a worker must not complete / request
    review before acceptance criteria are met. ``blocked`` gets its own
    guidance; any other non-``done`` verdict gets the ``continue`` guidance.
    A broken judge fails open (logged) so it cannot permanently wedge work."""
    if not task or not task.goal_mode or not _goal_judge_available():
        return
    try:
        # Headless gate runs outside any agent turn: bind the per-task relay-affinity scope
        # (mirrors kanban_specify) so the relay does not reject the judge call (#113669).
        from agent.portal_tags import get_affinity_scope, reset_affinity_scope, set_affinity_scope
        affinity_token = None if get_affinity_scope() else set_affinity_scope(f"kanban:{tid}")
        try:
            verdict, reason, parse_failed, _, transport_failed = judge_goal(
                goal=handoff_judge_goal(task.title, task.body, tool_name),
                last_response=evidence.strip())
        finally:
            if affinity_token is not None:
                reset_affinity_scope(affinity_token)
    except Exception as judge_exc:
        logger.warning(
            "goal judge check failed, allowing lifecycle handoff: %s", judge_exc, exc_info=True)
        return
    if transport_failed:
        # ``judge_goal`` fails open to ``continue`` on transport errors (relay 400, auth, timeout);
        # an unreachable judge is not a human "not done" and must not reject the handoff (#83610).
        logger.warning("goal judge unreachable (%s), allowing lifecycle handoff", reason)
        return
    if verdict == "done":
        if parse_failed or tool_name != 'kanban_complete':
            return
        from hermes_cli.kanban_goal_receipt import GoalVerification
        return GoalVerification.for_handoff(task, evidence)
    if verdict == 'blocked' and not parse_failed:
        _kc.park_unachievable(task, tid, reason, conn)
    key = "blocked" if verdict == "blocked" else "continue"
    raise _Reject(_kc.GOAL_GATE_MESSAGES[tool_name][key].format(reason=reason, tid=tid), "goal_judge")


# --- Runtime-activity → board bridges (auto-heartbeat, live comment injection) ---
# The dispatcher watchdog reads ``tasks.last_heartbeat_at``, not the agent's in-process
# activity timestamp, so normal work is mirrored onto the board here (``kanban_heartbeat``
# stays for notes / pre-extending a claim). Best-effort: never raise into the agent loop;
# rate-limited per process (a race costs one harmless extra write); no-op outside a
# dispatcher-spawned worker.

# --------------------------------------------------------------------------- Runtime-activity →
# board-heartbeat bridge (#31752)
# --------------------------------------------------------------------------- When the agent ticks
# ``_touch_activity`` during normal work (between tool calls, mid-stream chunks, etc.), we want the kanban
# board's ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher watchdog (which reads
# ``tasks.last_heartbeat_at``, not the agent's in-process timestamp) doesn't reclaim an actively-running
# worker as stale. The model is not required to call the explicit ``kanban_heartbeat`` tool for this to work
# — that tool stays available for workers that want to attach a note or pre-emptively extend a claim across
# a known-long op. Constraints: - Best-effort: never raise. The agent loop must not care if the bridge fails
# (board missing, DB locked, etc.). - Rate-limited to one DB write per 60s per-process; runtime activity can
# tick on every chunk/tool result and we don't need that resolution. - No-op outside dispatcher-spawned
# worker context (no ``HERMES_KANBAN_TASK``). - No durable note on these auto-heartbeats; that's reserved
# for the explicit tool which carries a model-supplied note.
_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0
_auto_heartbeat_fence_warned = False


def register_current_worker_from_env() -> bool:
    """Record this worker's pid on its run when the dispatcher died before it could
    (``adopt_worker_pid``). False only when the board says the run was already reclaimed:
    the caller must exit. Anything unreadable (no run id, delegate child, board error)
    lets the worker run, as before."""
    tid = os.environ.get("HERMES_KANBAN_TASK")
    run_id = _worker_run_id(tid) if tid else None
    if run_id is None or _is_delegated_child_context():
        return True
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        with _board(None, quiet_close=True) as (_kb, conn):
            return kbd.adopt_worker_pid(conn, tid, run_id, os.getpid())
    except Exception:
        logger.debug("kanban worker registration for %s failed", tid, exc_info=True)
        return True


def heartbeat_current_worker_from_env() -> bool:
    """Claim extension + board heartbeat for the current worker; True iff both writes
    succeed. ``HERMES_KANBAN_RUN_ID`` pins the run row so a reclaimed stale run is not
    heartbeated; ``HERMES_KANBAN_CLAIM_LOCK`` absent -> default claimer (local workers)."""
    global _auto_heartbeat_last_attempt, _auto_heartbeat_fence_warned
    tid = os.environ.get("HERMES_KANBAN_TASK")
    now = time.monotonic()
    if not tid or (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    if _is_delegated_child_context():
        # An in-process delegate child's activity is not the worker's liveness; checked before
        # stamping the window so a chatty child cannot starve the worker's own heartbeat.
        return False
    _auto_heartbeat_last_attempt = now
    try:
        from hermes_cli import kanban_db_dispatch as kbd
        with _board(None, quiet_close=True) as (kb, conn):
            ops = ((kb.heartbeat_claim, {"claimer": os.environ.get("HERMES_KANBAN_CLAIM_LOCK")}),
                   (kbd.heartbeat_worker, {"note": None, "expected_run_id": _worker_run_id(tid)}))
            succeeded = True
            for fn, kwargs in ops:
                op = fn.__name__
                try:
                    succeeded = bool(fn(conn, tid, **kwargs)) and succeeded
                except PermissionError as exc:
                    # The board fence rejected the worker's own liveness write: this process
                    # inherited HERMES_DELEGATED_CHILD_CONTEXT next to HERMES_KANBAN_TASK, so it is
                    # a delegate descendant, not the dispatcher's worker (kanban_complete refuses
                    # too). Loud once: at DEBUG the board just showed a worker that never beats.
                    succeeded = False
                    if not _auto_heartbeat_fence_warned:
                        _auto_heartbeat_fence_warned = True
                        logger.warning(
                            "kanban auto-heartbeat for task %s refused (%s): this process carries "
                            "HERMES_DELEGATED_CHILD_CONTEXT together with HERMES_KANBAN_TASK, so the board "
                            "treats it as a delegate_task descendant and its claim will not be extended by "
                            "activity. Only the dispatcher's own spawn grants worker scope; do not copy a "
                            "worker's environment into a hand-launched process.", tid, exc)
                except Exception:
                    logger.debug("auto-heartbeat: %s failed", op, exc_info=True)
                    succeeded = False
        return succeeded
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


# Live operator-note injection: poll the task for new comments and steer them in
# OUT-OF-BAND, so a user can talk to a running task without block → comment → unblock.
# Watermarked per task, seeded on first poll at the run's start (earlier history is in the prompt).
_COMMENT_POLL_MIN_INTERVAL_SECONDS = 6.0
_comment_poll_last_attempt: float = 0.0
_comment_watermark: dict[str, int] = {}


def inject_new_comments_from_env(agent: Any) -> bool:
    """Steer new operator comments on the worker's task into ``agent``; True iff a
    steer was injected; never raises. Skipped as own: the comments THIS run wrote, by id
    (``commented`` events carrying ``writer_run_id``; tool and CLI alike). A same-profile
    chat's note is an operator note. Without a run id, falls back to the author filter."""
    global _comment_poll_last_attempt
    # Operator notes address the dispatcher-owned worker; a delegate_task child sharing
    # this process must neither receive them nor advance the shared watermark (#112817).
    tid = os.environ.get("HERMES_KANBAN_TASK") if _is_dispatcher_owned_worker() else None
    now = time.monotonic()
    if (not tid or agent is None or not hasattr(agent, "steer")
            or (now - _comment_poll_last_attempt) < _COMMENT_POLL_MIN_INTERVAL_SECONDS):
        return False
    _comment_poll_last_attempt = now
    seen = _comment_watermark.get(tid)
    run_id = _worker_run_id(tid)
    own_ids: set[int] = set()
    try:
        with _board(None, quiet_close=True) as (kb, conn):
            if seen is None and run_id is not None:
                # [daveey] Seed at the run's start, not "now": comments from before the run
                # are in the prompt, but ones posted between spawn and this first poll are not.
                seen = kb.comment_id_before_run_start(conn, tid, run_id)
                if seen is not None:
                    _comment_watermark[tid] = seen
            rows = kb.list_comments_after(conn, tid, after_id=seen or 0)
            if seen is not None and rows and run_id is not None:
                own_ids = (kb.own_comment_ids(conn, tid, run_id)
                           | kb.delivered_comment_ids(conn, tid, run_id))
            agent_ids = kb.agent_comment_ids(conn, tid, [c.id for c in rows]) if seen is not None else set()
    except Exception:
        logger.debug("comment-inject: bridge failed", exc_info=True)
        return False
    if seen is None:  # no run to anchor on: everything so far counts as already seen
        _comment_watermark[tid] = max((c.id for c in rows), default=0)
    if seen is None or not rows:
        return False
    # Advance past everything read (including our own notes) so nothing is re-injected.
    _comment_watermark[tid] = max(c.id for c in rows)
    # Same resolution the write side used, for the stamp and the run-less fallback.
    own = _persisted_identity()
    if run_id is not None:
        # [daveey] Own = written by this run (by id). Every actor usually shares one
        # profile, so an author filter dropped every operator note from a same-profile chat.
        fresh = [c for c in rows if c.id not in own_ids and (c.body or "").strip()]
    else:
        fresh = [c for c in rows if (c.author or "").strip() != own and (c.body or "").strip()]
    if not fresh:
        return False
    from tools.kanban_tools_notes import live_note
    note = live_note(fresh, own, agent_ids)
    try:
        delivered = bool(agent.steer(note))
    except Exception:
        logger.debug("comment-inject: steer failed", exc_info=True)
        return False
    if delivered and run_id is not None:
        try:
            with _board(None, quiet_close=True) as (kb, conn):
                kb.record_comment_delivery(conn, tid, run_id, [c.id for c in fresh])
        except Exception:
            logger.debug("comment-inject: delivery receipt failed", exc_info=True)
    return delivered


# Soft deadline: near ``max_runtime_seconds`` steer the running turn to checkpoint with
# kanban_continue instead of being SIGTERMed mid-work by enforce_max_runtime. Two notices per
# run: at ``kanban.soft_deadline_fraction`` of the cap and a final one two thirds of the way
# from there to the cap. The deadline is read from the board (live max_runtime, active run's
# started_at) with the dispatcher's own wall clock, so both sides agree on "elapsed".
_SOFT_DEADLINE_POLL_MIN_INTERVAL_SECONDS = 15.0
_soft_deadline_last_attempt: float = 0.0
_soft_deadline_sent: dict[tuple[str, int], int] = {}  # (task, run) -> notices delivered


def inject_soft_deadline_from_env(agent: Any, *, now: Optional[float] = None) -> bool:
    """Steer a soft-deadline notice into ``agent`` when due; True iff one was injected."""
    global _soft_deadline_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK") if _is_dispatcher_owned_worker() else None
    mono = time.monotonic()
    if (not tid or agent is None or not hasattr(agent, "steer")
            or (mono - _soft_deadline_last_attempt) < _SOFT_DEADLINE_POLL_MIN_INTERVAL_SECONDS):
        return False
    _soft_deadline_last_attempt = mono
    run_id = _worker_run_id(tid)
    if run_id is None or _soft_deadline_sent.get((tid, run_id), 0) >= 2:
        return False
    try:
        from hermes_cli import kanban_continuation as kc
        fraction = kc.soft_deadline_fraction()
        if fraction <= 0:
            return False
        with _board(None, quiet_close=True) as (kb, conn):
            row = conn.execute(
                "SELECT t.max_runtime_seconds, r.started_at FROM tasks t "
                "JOIN task_runs r ON r.id = t.current_run_id "
                "WHERE t.id = ? AND t.current_run_id = ? AND t.status = 'running'",
                (tid, run_id),
            ).fetchone()
            started = kb._to_epoch(row["started_at"]) if row else None
        if not row or not row["max_runtime_seconds"] or started is None:
            return False
        limit = int(row["max_runtime_seconds"])
        elapsed = int((time.time() if now is None else now) - started)
        soft_at = int(limit * fraction)
        final_at = soft_at + (limit - soft_at) * 2 // 3
        sent = _soft_deadline_sent.get((tid, run_id), 0)
        due = 2 if elapsed >= final_at else 1 if elapsed >= soft_at else 0
        if due <= sent:
            return False
        note = kc.soft_deadline_message(elapsed=elapsed, limit=limit, final=due == 2)
        _soft_deadline_sent[(tid, run_id)] = due
        return bool(agent.steer(note))
    except Exception:
        logger.debug("soft-deadline inject failed", exc_info=True)
        return False


# --- Handlers ---

@_kanban_handler("kanban_show")
def _handle_show(args: dict, **kw) -> str:
    """Full task state: row, parents, children, comments, runs, last 50 events."""
    from tools.kanban_show import show
    return show(args)


@_kanban_handler("kanban_list")
def _handle_list(args: dict, **kw) -> str:
    """Task summaries with the same core filters as the CLI."""
    _require_orchestrator_tool("kanban_list")
    include_archived = _parse_bool_arg(args, "include_archived")
    limit = args.get("limit")
    try:
        limit = KANBAN_LIST_DEFAULT_LIMIT if limit is None else int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    _check(limit >= 1, "limit must be >= 1")
    _check(limit <= KANBAN_LIST_MAX_LIMIT, f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    with _board(args.get("board")) as (kb, conn):
        # Match CLI list: dependencies cleared since the last dispatcher tick
        # should be visible to orchestrators immediately.
        promoted = kb.recompute_ready(conn)
        # One extra row lets the output report truncation without dumping the board.
        rows = kb.list_tasks(
            conn, assignee=args.get("assignee"), status=args.get("status"),
            tenant=args.get("tenant"), include_archived=include_archived, limit=limit + 1)
        truncated = len(rows) > limit
        tasks = rows[:limit]
        return json.dumps({
            "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
            "count": len(tasks), "limit": limit, "truncated": truncated,
            "next_limit": (min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                           if truncated and limit < KANBAN_LIST_MAX_LIMIT else None),
            "promoted": promoted})


@_kanban_handler("kanban_complete", classify_rejections=True)
def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff. Every refusal
    carries a ``rejection`` class from ``COMPLETE_REJECTION_CLASSES``."""
    try:
        tid = _worker_guard("kanban_complete", args)
    except _Reject as e:
        raise _Reject(json.loads(e.args[0])["error"], e.rejection or "not_owner") from None
    summary = _redact_opt(args.get("summary"))
    result = _redact_opt(args.get("result"))
    metadata = args.get("metadata")
    if isinstance(metadata, dict):
        # Keep the unredacted dict if the redacted JSON cannot be re-parsed.
        metadata = _redact_metadata(metadata) or metadata
    created_cards = _coerce_str_list(
        args.get("created_cards"), "created_cards", "task ids", strip=True)
    artifacts = _coerce_str_list(args.get("artifacts"), "artifacts", "file paths", strip=True)
    if artifacts:
        metadata = _merge_artifacts(metadata, artifacts)
    _check(summary or result, "provide at least one of: summary (preferred), result", "empty")
    _require_dict_metadata(metadata)
    metadata = _stamp_worker_session_metadata(tid, metadata)
    with _board(args.get("board")) as (kb, conn):
        from hermes_cli import kanban_foreign_card as _kfc
        foreign = _kfc.refusal(conn, tid)  # [daveey] only a replica copy here: name the owner, not "stale run"
        if foreign:
            raise _Reject(foreign, _kfc.REJECTION)
        # Goal-mode pre-completion judge gate (Issue #38367). Prevent workers from bypassing the auxiliary
        # judge by calling kanban_complete before acceptance criteria are met. Only enforce when a judge is
        # actually reachable — see _goal_judge_available for why an unavailable judge fails open.
        task = kb.get_task(conn, tid)
        goal_verification = _goal_gate("kanban_complete", task, tid, (summary or result or "").strip(), conn=conn)
        try:
            run_id = _worker_run_id(tid)
            if run_id is not None and _kc.parked_by_own_run(conn, tid, run_id):
                # This worker parked its card with kanban_block, then edited its own edges (kanban_unlink):
                # its run is over but nothing has claimed the card since, so it may still close its own card.
                run_id = None
            event_mark = _kc.event_mark(conn, tid)  # a refusal's own pr_acceptance event lands above it
            ok = kb.complete_task(
                conn, tid, result=result, summary=summary, metadata=metadata,
                created_cards=created_cards, expected_run_id=run_id, goal_verification=goal_verification)
        except kb.ArtifactPreservationError as artifact_err:
            # Structured rejection — surface the phantom ids so the worker can retry with a corrected list
            # or drop the field. Audit event already landed in the DB. The task itself was NOT mutated (the
            # gate runs before the write txn), so the worker can simply call kanban_complete again. Spell
            # that out — without it the model often interprets a tool_error as a terminal failure and either
            # blocks or crashes the run instead of retrying. See #22923.
            return tool_error(
                f"kanban_complete could not preserve the declared artifacts: {artifact_err}. "
                f"Your task is still in-flight and its scratch workspace was kept. Fix the "
                f"artifact path or storage error, then retry kanban_complete with the same "
                f"handoff.", rejection="artifacts")
        except kb.LiveClaimError as claim_err:
            # Env-less caller (orchestrator, another session) on a card a dispatcher
            # worker is executing: refusing here is what keeps that worker's run open.
            return tool_error(
                f"kanban_complete refused: {claim_err}. Nothing changed. Wait for the worker "
                f"to finish, or an operator can run `hermes kanban complete --force {tid}`.",
                rejection="live_claim")
        except kb.HallucinatedCardsError as hall_err:
            # The gate runs before the write txn, so the task was NOT mutated;
            # say so explicitly or the model treats the error as terminal and
            # blocks/crashes instead of retrying. Audit event already landed.
            return tool_error(
                f"kanban_complete blocked: the following created_cards do not exist or were not "
                f"created by this worker: {', '.join(hall_err.phantom)}. Your task is still "
                f"in-flight (no state change). Retry kanban_complete with the same "
                f"summary/metadata and either drop these ids from created_cards, or pass "
                f"created_cards=[] to skip the card-claim check entirely.",
                rejection="phantom_cards")
        except kb.EmptyCompletionError as empty_err:
            # Same shape as the card gate: nothing was mutated, the audit event
            # already landed; the worker retries with evidence instead of stalling.
            return tool_error(
                f"kanban_complete blocked: {empty_err}. Your task is still in-flight (no state "
                f"change). Retry kanban_complete with a non-empty summary or result describing "
                f"what was done.", rejection="empty")
        task = kb.get_task(conn, tid)
        if not ok:
            # complete_task reports every refusal as bare False; a reopened or
            # never-finished parent is the actionable one. Name the blockers so
            # the worker/operator completes the parents instead of re-running.
            blockers = kb.unsatisfied_parents(conn, tid)
            if blockers:
                detail = ", ".join(f"{pid} ({status})" for pid, status in blockers)
                raise _Reject(
                    f"could not complete {tid}: unsatisfied parent dependencies: "
                    f"{detail}; complete the parents first (done or archived)", "parents")
            if task and task.status in ("scheduled", "todo", "ready") and not _kc.pr_acceptance_refused(
                    conn, tid, event_mark):
                from hermes_cli.kanban_watch import NOT_RUNNING_TEMPLATE  # [daveey] not a vague "stale run"
                raise _Reject(NOT_RUNNING_TEMPLATE.format(tid=tid, status=task.status), "not_running")
            reason = (task.last_failure_error if task else None) or \
                f"could not complete {tid} (unknown id, stale run, or already terminal)"
            _check(False, reason, "pr_acceptance" if _kc.pr_acceptance_refused(conn, tid, event_mark)
                   else "stale_run")
        run = kb.latest_run(conn, tid)
        # Artifact staging is atomic with the completion write, so a worker that
        # read `kanban_attachments` before completing saw an empty list and has
        # no way to observe what its completion just registered (#117360).
        # Report the card's durable attachment set in the result.
        return _ok(task_id=tid, run_id=run.id if run else None,
                   attachments=[
                       _fields(a, _ATTACHMENT_FIELDS)
                       for a in kb.list_attachments(conn, tid)])


def _awaiting_arg(args: dict, wake: tuple) -> tuple[Optional[dict], tuple]:
    """``(awaiting, wake)``: the declared producer (``kanban_awaiting``), and the park's probe
    with ``hermes kanban wait-check`` filled in for a card wait that brought none. [daveey]"""
    from hermes_cli import kanban_awaiting as kaw

    try:
        awaiting = kaw.normalize(args.get("awaiting"))
    except kaw.AwaitingError as exc:
        raise _Reject(str(exc), exc.code) from exc
    if awaiting and awaiting["kind"] == "human":
        exc = kaw.human_error(os.environ.get("HERMES_KANBAN_TASK") or "",
                              " ".join(str(args.get(k) or "") for k in ("reason", "checkpoint")) + " "
                              + str(awaiting.get("why") or ""))
        raise _Reject(str(exc), exc.code)
    probe = None if wake[0] else kaw.card_probe(awaiting)
    if probe:
        from hermes_cli import kanban_wake_check as kwc
        wake = (probe, kwc.normalize_every(None), wake[2])
    try:
        kaw.confirm_remote(awaiting)  # ssh, so before the park's write txn
    except kaw.AwaitingError as exc:
        raise _Reject(str(exc), exc.code) from exc
    return awaiting, wake


def _route_needs(op: str, args: dict, tid: str, reason: Optional[str]) -> Optional[str]:
    """Cross-gateway capability routing for ``needs`` on block/schedule; None when absent. [daveey]"""
    needs = args.get("needs")
    if needs in (None, [], ""):
        return None
    from hermes_cli import kanban_capability_routing as kcr
    try:
        needs = kcr.reg.normalize_needs(needs)
    except ValueError as exc:
        raise _Reject(str(exc), "invalid_args") from None
    _check(args.get("human_request") is None,
           "needs routes the wall to the gateway that holds the capability; a human_request asks David. "
           "Pass one: if only David can grant it, drop needs.")
    _check(not args.get("blocker_id"), "needs cannot be combined with blocker_id")
    _check(not args.get("wake_check") and not args.get("clock_only"),
           "needs parks on the owner card's own wake probe; drop wake_check/clock_only")
    with _board(args.get("board")) as (kb, conn):
        task = kb.get_task(conn, tid)
        _check(not (task and task.goal_mode),
               "goal_mode tasks cannot park on an owner card; finish with kanban_complete")
        try:
            res = kcr.route(conn, tid, needs, reason=reason or "", expected_run_id=_worker_run_id(tid),
                            board=args.get("board"))
        except kcr.CapabilityRouteError as exc:
            raise _Reject(str(exc), exc.code) from None
        return _ok_landed(kb, conn, tid, "scheduled", requested_kind=args.get("kind"), op=op,
                          capability_routes=res["routes"], wake_check=res["wake_check"],
                          note=("Parked on the capability owner card(s) above; it wakes when they are "
                                "done. Do not ask David: the owner card does, if the action exceeds "
                                "the approved scope. End your turn."))


STANDING_WAIT_EVENT = "standing_wait_hold"


def _standing_wait_guard(kb, conn, tid: str, kind: Optional[str], human_request: Any, reason: str) -> None:
    """Hold a block on the user ONCE when the card carries a standing "don't wait". [daveey]

    A needs_input block (or one with a human_request) under such a standing line is refused with the
    words quoted back and a ``standing_wait_hold`` event (coach/self-heal read it). Calling again with
    the block lands it: a genuine human-only need (a credential, a physical step) is never trapped.
    """
    if kind != "needs_input" and not human_request:
        return
    from agent import standing_instructions as si
    task = kb.get_task(conn, tid)
    found = si.explicit_lines(getattr(task, "body", "") or "")
    for c in kb.list_comments(conn, tid):
        found += si.explicit_lines(c.body or "")
    dont_wait = si.says_dont_wait(si.dedupe(found))
    if not dont_wait:
        return
    run_id = _worker_run_id(tid)
    if any(e.kind == STANDING_WAIT_EVENT and (run_id is None or e.run_id in (None, run_id))
           for e in kb.list_events(conn, tid)):
        return  # already held once this run: the agent re-confirmed a human-only need
    with kb.write_txn(conn, allow_nested=True):
        kb._append_event(conn, tid, STANDING_WAIT_EVENT,
                         {"kind": kind, "standing": dont_wait[:3], "reason": (reason or "")[:300]}, run_id=run_id)
    quoted = "; ".join(f'"{w}"' for w in dont_wait[:3])
    raise _Reject(
        f"Not blocked: this card carries the user's standing instruction {quoted}. Decide it yourself and keep "
        "going (pick the option the user's goal implies, record the choice in a kanban_comment). A timing or "
        "consent question is not a reason to wait. Only if this truly needs a human (a credential, a physical "
        "step, money beyond the approved budget) call kanban_block again with the same reason; that lands it.",
        "standing_dont_wait")


@_kanban_handler("kanban_block")
def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _worker_guard("kanban_block", args)
    reason = _redact(
        _require_text(args, "reason", "reason is required — explain what input you need"))
    kind = args.get("kind")
    routed = _route_needs("block", args, tid, reason)
    if routed is not None:
        return routed
    with _board(args.get("board")) as (kb, conn):
        _check(kind is None or kind in kb.VALID_BLOCK_KINDS,
               f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)")
        # The goal loop treats ANY blocked status as terminal, so kanban_block
        # would be an escape hatch around the completion judge: goal_mode tasks
        # may only block on genuine external blockers.
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete judge gate in #38367).
        # kanban_block is a second exit path out of the goal loop — run_kanban_goal_loop() treats ANY
        # `blocked` status as terminal, identically to `done`, regardless of kind. Without this, a worker
        # that learns kanban_complete is gated can just call kanban_block(reason="anything") to escape the
        # loop instead. Restrict goal_mode tasks to the kinds that represent a genuine external blocker the
        # worker cannot resolve itself; `capability` and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        _check(not (task and task.goal_mode and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS),
               f"goal_mode tasks can only block with kind in "
               f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). If the task is actually "
               f"finished or cannot proceed for another reason, call kanban_complete instead — "
               f"the completion judge will evaluate it.")
        blocker_id = _blocker_id_arg(args)
        clear_blocker = _clear_blocker_arg(args, blocker_id)
        wake = _wake_check_args(args)
        awaiting, wake = _awaiting_arg(args, wake)
        from hermes_cli.kanban_block_policy import ERROR_CODE, block_policy_error
        _policy = block_policy_error(kind, human_request=args.get("human_request"), wake_check=wake[0],
                                     clock_only=wake[2], blocker_id=blocker_id, reason=reason, task=(conn, tid))
        _check(_policy is None, _policy or "", ERROR_CODE)
        _standing_wait_guard(kb, conn, tid, kind, args.get("human_request"), reason)
        _park_guard(conn, tid, "block", wake, reason=reason, kind=kind, blocker_id=blocker_id,
                    human_request=args.get("human_request"))
        _check(*ask_checks(args.get("human_request"), context=filing_context(conn, tid)))  # lint + probe [daveey]
        ok = kb.block_task(conn, tid, reason=reason, kind=kind, expected_run_id=_worker_run_id(tid),
                           blocker_id=blocker_id, wake_check=wake[0], check_every=wake[1], awaiting=awaiting,
                           clock_only=wake[2], human_request=kb.redact_review_value(args.get("human_request")),
                           clear_blocker=clear_blocker, **_blocker_scope_args(args))
        _check(ok, f"could not block {tid} (unknown id or not in running/ready/triage)")
        escalated = _escalated(kb, conn, tid)
        if escalated:
            return escalated
        landed_kind = kb.get_task(conn, tid).block_kind
        extra: dict = {"block_kind": landed_kind}
        if blocker_id:
            extra["blocker_id"] = blocker_id
        landed_status = kb.get_task(conn, tid).status
        if landed_status == "scheduled":
            # A wait on time, not on a human: parked with a wake time (kanban_timed_park). [daveey]
            from hermes_cli.kanban_continuation import pending_wake
            parked = pending_wake(conn, tid) or {}
            note = ("Parked as a timed wait (scheduled), not a human block: it wakes when "
                    "wake_check exits 0, at wake_at, or earlier on `hermes kanban unblock`.")
            if parked.get("blocker_wake"):
                note = (f"Parked behind shared blocker `{blocker_id}` (scheduled): it wakes at wake_at "
                        "(the shared probe interval) to probe the wall, or as soon as the blocker clears.")
            return _ok_landed(kb, conn, tid, "scheduled", requested_kind=kind, wake_at=parked.get("wake_at"),
                              **_wake_check_echo(wake), **({"blocker_id": blocker_id} if blocker_id else {}),
                              note=note)
        if kind == "dependency" and landed_kind != kind:
            # block_task re-kinds a dependency wait that no open parent can satisfy.
            extra["requested_kind"] = kind
            extra["note"] = (
                "kind='dependency' only waits on an incomplete parent; no parent is open, "
                "so this was recorded as needs_input (sticky until a human unblocks) "
                "instead of parking in todo where the dispatcher would respawn it."
            )
        return _ok_landed(kb, conn, tid, "blocked", **extra)


@_kanban_handler("kanban_schedule")
def _handle_schedule(args: dict, **kw) -> str:
    """Park the current task until an orchestrator re-gates it."""
    tid = _worker_guard("kanban_schedule", args)
    raw_reason = args.get("reason")
    _check(raw_reason is None or isinstance(raw_reason, str), "reason must be a string")
    reason = _redact(raw_reason.strip()) if raw_reason and raw_reason.strip() else None
    routed = _route_needs("schedule", args, tid, reason)
    if routed is not None:
        return routed
    with _board(args.get("board")) as (kb, conn):
        # The goal loop treats ``scheduled`` as terminal like ``blocked``, so
        # parking would bypass the completion judge (see kanban_block, #38696).
        task = kb.get_task(conn, tid)
        _check(not (task and task.goal_mode),
               "goal_mode tasks cannot be scheduled: for a wait on time or a cron use "
               "kanban_block with kind='time' (SCHEDULED_UNTIL=<ISO8601> in the reason sets "
               "the wake), or kanban_complete so the completion judge can evaluate it.")
        wake = _wake_check_args(args)
        awaiting, wake = _awaiting_arg(args, wake)
        blocker_id = _blocker_id_arg(args)
        clear_blocker = _clear_blocker_arg(args, blocker_id)
        _park_guard(conn, tid, "schedule", wake, reason=reason)
        from hermes_cli import kanban_watch as kwatch
        raw_watch = args.get("watch")
        _check(raw_watch is None or isinstance(raw_watch, bool), "watch must be true or false")
        ok = kb.schedule_task(
            conn, tid, watch=kwatch.resolve(conn, tid, raw_watch), reason=reason,
            expected_run_id=_worker_run_id(tid),
            wake_check=wake[0], check_every=wake[1], clock_only=wake[2], awaiting=awaiting,
            clear_blocker=clear_blocker, blocker_id=blocker_id, **_blocker_scope_args(args))
        _check(ok, "" if ok else krc.human_answer_refusal(conn, tid, "schedule")
               or f"could not schedule {tid} (unknown id, or not in todo/ready/running/blocked/triage)")
        escalated = _escalated(kb, conn, tid)
        if escalated:
            return escalated
        # [daveey] A watch says so; a long non-watch park says which chat it holds open and how to free it.
        hold = kwatch.chat_hold_hint(conn, tid)
        return _ok_landed(kb, conn, tid, "scheduled", reason=reason, **_wake_check_echo(wake),
                          **({"watch": True} if kwatch.is_watch(conn, tid) else {}),
                          **({"chat_hold": hold} if hold else {}))


@_kanban_handler("kanban_continue")
def _handle_continue(args: dict, **kw) -> str:
    """Checkpoint + end this run; the dispatcher re-queues the card for a fresh run."""
    tid = _worker_guard("kanban_continue", args)
    checkpoint = _redact(_require_text(
        args, "checkpoint", "checkpoint is required — write what is done, current state, next "
        "steps and background jobs to re-adopt so the next run can resume"))
    raw_after = args.get("resume_after_seconds", 0)
    _check(raw_after is None or (isinstance(raw_after, (int, float)) and not isinstance(raw_after, bool)),
           "resume_after_seconds must be an integer number of seconds")
    metadata = args.get("metadata")
    _require_dict_metadata(metadata)
    if metadata is not None:
        metadata = _redact_metadata(metadata)
        _check(metadata is not None, "metadata could not be safely serialized")
    metadata = _stamp_worker_session_metadata(tid, metadata)
    blocker_id = _blocker_id_arg(args)
    if blocker_id:
        metadata = {**(metadata or {}), "blocker_id": blocker_id}
    if _clear_blocker_arg(args, blocker_id):
        from hermes_cli.kanban_shared_blockers import CLEAR_FLAG
        metadata = {**(metadata or {}), CLEAR_FLAG: True}
    from hermes_cli.kanban_continuation import continue_task
    with _board(args.get("board")) as (kb, conn):
        wake = _wake_check_args(args)
        awaiting, wake = _awaiting_arg(args, wake)
        _park_guard(conn, tid, "continue", wake, resume_after=int(raw_after or 0))
        res = continue_task(conn, tid, checkpoint=checkpoint, resume_after_seconds=int(raw_after or 0),
                            expected_run_id=_worker_run_id(tid), metadata=metadata, awaiting=awaiting,
                            wake_check=wake[0], check_every=wake[1], clock_only=wake[2],
                            **_blocker_scope_args(args))
        from hermes_cli.kanban_awaiting import CODES
        _check(res.get("ok"), f"could not continue {tid}: {res.get('error')}",
               res.get("reason_code") if res.get("reason_code") in CODES else None)
        escalated = _escalated(kb, conn, tid)
        if escalated:
            return escalated
        note = ("Checkpoint saved; a fresh run will resume this card"
                + (f" after {int(raw_after)}s" if raw_after else " now")
                + ". End your turn with a one-line reply; do not call more tools.")
        return _ok_landed(kb, conn, tid, res["status"], continuation=res["continuation"],
                          wake_at=res.get("wake_at"), next=note)


@_kanban_handler("kanban_move")
def _handle_move(args: dict, **kw) -> str:
    """[daveey] Checkpoint, end this run and re-queue the card with new runs_on labels (hermes_cli.kanban_move).
    Another card: only the origin repin of a worker-first card the caller's task created (kanban_creator_repin)."""
    from hermes_cli import kanban_creator_repin as kcr
    if kcr.other_card(args):
        return kcr.tool_repin(args, board=_board, check=_check, ok=_ok, reject_child=_reject_delegated_child_mutation)
    tid = _worker_guard("kanban_move", args)
    checkpoint = _redact(_require_text(
        args, "checkpoint", "checkpoint is required — write what is done, what is not portable and the next "
        "steps so the run on the other machine can resume"))
    reason = _require_text(args, "reason", "reason is required — say what this machine lacks")
    runs_on = args.get("runs_on")
    _check(isinstance(runs_on, list) and runs_on, "runs_on must be a non-empty list of labels, e.g. ['machine:worker-1']")
    from hermes_cli.kanban_move import move_task
    with _board(args.get("board")) as (kb, conn):
        res = move_task(conn, tid, runs_on=runs_on, reason=reason, checkpoint=checkpoint,
                        expected_run_id=_worker_run_id(tid), force=bool(args.get("force")))
        _check(res.get("ok"), f"could not move {tid}: {res.get('error')}")
        note = (f"Moved: the card is re-queued for {res['runs_on']} (eligible now: {', '.join(res['matches'])}); "
                "this run is over. End your turn with a one-line reply; do not call more tools.")
        return _ok_landed(kb, conn, tid, res["status"], runs_on=res["runs_on"], matches=res["matches"],
                          move_no=res["move_no"], next=note)


@_kanban_handler("kanban_request_review")
def _handle_request_review(args: dict, **kw) -> str:
    """Move implementation into the first-class review phase."""
    tid = _worker_guard("kanban_request_review", args)
    summary = _redact(_require_text(
        args, "summary", "summary is required — describe what was implemented and how it "
        "was verified so the reviewer has context"))
    metadata = args.get("metadata")
    _require_dict_metadata(metadata)
    if metadata is not None:
        metadata = _redact_metadata(metadata)
        _check(metadata is not None, "metadata could not be safely serialized")
    artifacts = _coerce_str_list(args.get("artifacts"), "artifacts", "file paths", strip=True)
    if artifacts:
        metadata = _merge_artifacts(metadata, artifacts)
    metadata = _stamp_worker_session_metadata(tid, metadata)
    # Reviewer is model-supplied free text stored durably on the event payload.
    reviewer = _redact_opt(args.get("reviewer") or None)
    if reviewer:
        from hermes_cli.profiles import list_profile_names, profile_exists

        # A non-profile reviewer would park the card in `review` on an assignee
        # the dispatcher can never spawn (#106163).
        _check(profile_exists(reviewer),
               f"reviewer profile {reviewer!r} is not installed. "
               f"Installed profiles: {', '.join(list_profile_names())}")
    with _board(args.get("board")) as (kb, conn):
        _goal_gate("kanban_request_review", kb.get_task(conn, tid), tid, summary, conn=conn)
        try:
            ok, fail_reason = kb.request_review(
                conn, tid, summary=summary, metadata=metadata, reviewer=reviewer,
                expected_run_id=_worker_run_id(tid), with_reason=True)
        except kb.ArtifactPreservationError as artifact_err:
            # Same contract as kanban_complete (#22923): the transition rolled
            # back, the task is untouched and retryable — say so explicitly or
            # the model treats the tool_error as terminal.
            return tool_error(
                f"kanban_request_review could not preserve the declared artifacts: {artifact_err}. "
                f"Your task is still in-flight (no state change) and its scratch workspace was "
                f"kept. Fix the artifact path or storage error, then retry "
                f"kanban_request_review with the same handoff.")
        _check(ok, f"could not request review for {tid}: "
                   f"{fail_reason or 'unknown id or not in running/ready'}")
        return _ok_landed(kb, conn, tid, "review")


@_kanban_handler("kanban_request_changes")
def _handle_request_changes(args: dict, **kw) -> str:
    """Return a reviewer-owned running task to its implementer."""
    tid = _worker_guard("kanban_request_changes", args)
    reason = _redact(
        _require_text(args, "reason", "reason is required — describe the changes needed"))
    with _board(args.get("board")) as (kb, conn):
        ok, detail = kb.request_changes(
            conn, tid, reason=reason, expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not request changes for {tid}: {detail or 'invalid review state'}")
        return _ok_landed(kb, conn, tid, "ready", implementer=detail)


@_kanban_handler("kanban_heartbeat")
def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal liveness: extend the claim TTL AND record a heartbeat event.
    Without the claim half, a worker blocked in one long tool call would still
    be reclaimed by ``release_stale_claims``."""
    tid = _worker_guard("kanban_heartbeat", args)
    from hermes_cli import kanban_db_dispatch as kbd
    with _board(args.get("board")) as (kb, conn):
        # The dispatcher pins HERMES_KANBAN_CLAIM_LOCK at spawn; the default
        # claimer covers locally-driven workers that bypassed the dispatcher.
        kb.heartbeat_claim(conn, tid, claimer=os.environ.get("HERMES_KANBAN_CLAIM_LOCK"))
        ok = kbd.heartbeat_worker(
            conn, tid, note=args.get("note"), expected_run_id=_worker_run_id(tid))
        _check(ok, f"could not heartbeat {tid} (unknown id or not running)")
        return _ok(task_id=tid)


@_kanban_handler("kanban_comment")
def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    _reject_delegated_child_mutation("kanban_comment", args)
    tid = args.get("task_id")
    if not tid:
        # A comment writes to the board, so a bare call must stay an error — but
        # point at kanban_list, not an env var a chat caller cannot set (#91431).
        raise _Reject(
            "task_id is required: comments are per-task and this session has no "
            "dispatcher-assigned task to default to. Discover task ids with "
            "kanban_list, or pass the task id you mean.")
    body = _redact(_require_text(args, "body"))
    # Author comes from the worker's runtime identity (``_persisted_identity``), never
    # caller args: comments are injected into future workers' system prompts, so an
    # args["author"] override could forge a directive from ``hermes-system``.
    # Cross-task commenting stays unrestricted — it is the handoff channel between tasks.
    # Comments are injected into the next worker's system prompt by ``build_worker_context`` as
    # ``**{author}** (timestamp): {body}`` — accepting an ``args["author"]`` override let a worker forge a
    # comment from an authoritative-looking name like ``hermes-system`` and poison the future-worker context
    # with what reads as a system directive. See #19713.
    author = _persisted_identity()
    with _board(args.get("board")) as (kb, conn):
        from hermes_cli import kanban_foreign_card as _kfc
        foreign = _kfc.refusal(conn, str(tid))  # [daveey] only a replica copy here: name the owner
        if foreign:
            raise _Reject(foreign, _kfc.REJECTION)
        # [daveey] This tool is always an agent: record it, so no reader guesses (agent-261aa31f49).
        from gateway.session_context import get_session_env
        cid = kb.add_comment(conn, tid, author=author, body=str(body), writer_kind="agent",
                             writer_session=get_session_env("HERMES_SESSION_ID", "") or None)
        subscribed = _maybe_subscribe_on_touch(conn, str(tid))
        live = kb.live_run_id(conn, str(tid))
        extra: dict = {"live_run": live is not None}
        if live is not None and _worker_run_id(str(tid)) != live:
            extra["delivery"] = (
                "the card has a live worker run: it gets this note at its next model call "
                "(often a few minutes); a comment_delivered event on the card confirms it")
        return _ok(task_id=tid, comment_id=cid, **extra,
                   **({"subscribed": True} if subscribed else {}))


def _store_attachment(board, tid, filename, data, content_type) -> str:
    """Store via ``kanban_db.store_attachment_bytes`` (shared size cap, per-task
    dir, metadata row) so agent, dashboard, and CLI surfaces stay in lockstep."""
    with _board(board) as (kb, conn):
        att_id = kb.store_attachment_bytes(
            conn, tid, str(filename), data,
            content_type=content_type, uploaded_by="agent", board=board)
        return _ok(task_id=tid, attachment_id=att_id, size=len(data))


@_kanban_handler("kanban_attach")
def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task."""
    tid = _worker_guard("kanban_attach", args)
    filename = _require_text(args, "filename")
    content_b64 = _require_text(args, "content_base64")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        raise _Reject(f"content_base64 is not valid base64: {e}")
    return _store_attachment(args.get("board"), tid, filename, data, args.get("content_type"))


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) capped at ``max_bytes`` -> ``(data, content_type)``.
    Every hop is SSRF-checked (redirects followed manually) so a model-controlled URL, or a
    public host 302ing, cannot reach loopback/private/cloud-metadata ranges. ``ValueError``
    for bad scheme, blocked target, too many redirects, or a body over the cap (checked
    while streaming, so nothing oversize is buffered)."""
    from urllib.parse import urljoin, urlparse
    import httpx
    from tools.url_safety import is_safe_url
    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"unsupported URL scheme {scheme!r}; only http/https are allowed")
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}")
        chunks: list[bytes] = []
        total = 0
        with httpx.stream("GET", current_url, headers={"User-Agent": "hermes-kanban/attach"},
                          timeout=30, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


@_kanban_handler("kanban_attach_url")
def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from an http(s) URL (shared size cap)."""
    from hermes_cli import kanban_db as kb
    tid = _worker_guard("kanban_attach_url", args)
    url = str(_require_text(args, "url")).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip() or "download"
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    return _store_attachment(
        args.get("board"), tid, filename, data, args.get("content_type") or fetched_ct)


@_kanban_handler("kanban_attachments")
def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _require_task_id(args)
    with _board(args.get("board")) as (kb, conn):
        _existing_task(kb, conn, tid)
        return json.dumps({
            "ok": True, "task_id": tid,
            "attachments": [
                _fields(a, _ATTACHMENT_FIELDS) for a in kb.list_attachments(conn, tid)]})


def _place_remote(args: dict, *, assignee: str, skills, parents, project_id, session_id, conn=None,
                  note_local_pin: bool = True, origin_profile=None) -> tuple[Optional[str], Optional[dict]]:
    """[daveey] Create the card on the gateway ``kanban.placement`` routes this one to.

    Returns a remote result/refusal, or local placement metadata. Worker-policy metadata
    must reach the initial task INSERT; local storage is not permission to execute.
    """
    try:
        from hermes_cli import kanban_placement as kp
        from hermes_cli import kanban_capability_registry as reg
        from hermes_cli.profiles import current_profile_name

        view = {**args, "assignee": assignee, "skills": skills, "parents": parents,
                "project_id": project_id}
        from hermes_cli import kanban_worker_node as _wn
        # runs_on only works on a board that syncs as primary; the machine check is kp._machine_picks_up.
        from hermes_cli import kanban_sync_runstate as _runstate  # down: a machine pin takes the gateway route
        machine_ok = ((lambda m: _wn.sync.board_mode(conn) == "primary" and kp._machine_picks_up(m)
                       and not _runstate.down()) if conn is not None else None)
        decision = kp.decide(view, machine_ok=machine_ok)
        target = decision["where"]
        if target == "refuse":
            return _placement_refused(decision["pin"], decision.get("why", "")), None
        if target == "local":
            local = _local_placement(kp, view, decision, machine_ok=machine_ok, note_local_pin=note_local_pin)
            stranded = _runstate.stranded_create((local or {}).get("runs_on"))
            return (tool_error(stranded, rejection="sync_not_running"), None) if stranded else (None, local)
        self_gw = reg.self_gateway() or "local"
        cfg = kp.config()
        placed = kp.create_remote(view, target=target, origin_session=session_id,
                                  origin_profile=origin_profile or current_profile_name("default") or "default",
                                  self_gw=self_gw, cfg=cfg, hub_cli=reg.hub_cli_path(cfg.get("hub_cli")))
        if not placed.get("ok"):
            logger.warning("kanban placement: %s refused the card (%s)",
                           target, placed.get("error"))
            if kp.explicit_foreign_pin(view, reg.self_gateway()):
                return _placement_refused(target, f"{target} refused the card: {placed.get('error')}"), None
            return None, None
        rid = placed["request_id"]
        # A chat model reads task_id null as "not created" and calls kanban_create again, which made a
        # duplicate card (10-27 drill). Fetch the target's answer for a bounded wait so the id comes
        # back like a local create's; otherwise say plainly that the card exists. [daveey]
        tid, remote_status = (None, None) if placed.get("queued_only") else kp.await_task_id(
            rid, hub_cli=reg.hub_cli_path(cfg.get("hub_cli")), budget=float(cfg.get("wait_seconds", 10) or 0))
        retry = ("This identical kanban_create call was already made, so no second card was sent; this is "
                 "the same card. " if placed.get("retry") else "")
        fields = {"placed": {"gateway": target, "request_id": rid, "task_id": tid, "origin_session": session_id,
                             "queued_only": bool(placed.get("queued_only")), "retry": bool(placed.get("retry"))}}
        if tid:
            shows = " and shows in this chat's tasks" if session_id else ""
            note = (f"{retry}Created on {target} as {tid}, not here: {decision['why']} (kanban.placement). It runs "
                    f"there{shows}. Read it with cross_task @task:default@{target}/{tid} or "
                    f"`hermes-hub tasks --gw {target}`.")
            return _ok(task_id=tid, status=remote_status or "created", **fields, runs_where=target,
                       note=note), None
        note = (f"{retry}The card IS created: it is queued on {target} as request {rid} and {target} makes the "
                f"card from it (it runs there, not here: {decision['why']}). Do NOT call kanban_create again "
                "for this card: an identical call returns this same request, and any changed call creates a "
                f"second, duplicate card. Its task id appears once {target} applies the request "
                + ("(this chat's tasks, " if session_id else "(")
                + "`hermes-hub requests`).")
        return _ok(task_id=None, status="queued", **fields, runs_where=target, note=note), None
    except Exception as exc:
        logger.warning("kanban placement failed", exc_info=True)
        try:
            from hermes_cli import kanban_placement as kp2
            from hermes_cli import kanban_capability_registry as reg2
            pin = kp2.explicit_foreign_pin(args, reg2.self_gateway())
        except Exception:
            pin = str(args.get("placement") or "").strip().lower()
            pin = "" if pin in ("", "auto", "local", "here") else pin
        if pin:
            return _placement_refused(pin, f"placement failed: {exc}"), None
        return None, None


def _creation_audit(args: dict, **effective) -> Optional[dict]:
    """Audit the same normalized fields placement judged, including inherited identity/project. [daveey]"""
    from hermes_cli.kanban_worker_placement import creation_audit

    return creation_audit({**args, **effective})


@_kanban_handler("kanban_create")
def _handle_create(args: dict, **kw) -> str:
    """Create a (child) task; orchestrator workers use this to fan out."""
    # Only an explicitly local create is board-scoped: any other placement or an origin binding can write a
    # real card on another gateway, which no local path check covers. [daveey]
    _reject_delegated_child_mutation(
        "kanban_create",
        args if args.get("placement") == "local" and not args.get("origin_session") else None)
    title = _require_text(args, "title")
    assignee = args.get("assignee") or _persisted_identity()
    # Workspace sharing is always explicit: omitted fields mean a fresh scratch workspace
    # even for a dispatcher-spawned creator (reusing the parent's path would let a child
    # mutate review evidence or race its checkout). Project identity is the one safe thing
    # to inherit implicitly (the DB turns it into a fresh per-task worktree).
    workspace_kind, workspace_path = args.get("workspace_kind"), args.get("workspace_path")
    # See #67567. ``project=""`` is an explicit "no project" (no ``or`` collapse, #106342).
    project_id = args["project"] if "project" in args else args.get("project_id")
    project_source_task_id = None
    triage, skills, goal_mode = (
        _parse_bool_arg(args, "triage"), _coerce_str_list(args.get("skills"), "skills", "skill names"),
        _parse_bool_arg(args, "goal_mode"))
    model_override, provider_override = args.get("model"), args.get("provider")
    _check(model_override or not provider_override, "'provider' requires 'model' to be set as well")
    max_retries = _ca.max_retries_arg(args)
    parents = _coerce_str_list(args.get("parents") or [], "parents", "task ids")
    # [daveey] Validate the origin binding BEFORE the card exists: a binding the origin gateway can never
    # read back is the silent "cards exist but the chat shows none" failure.
    from hermes_cli import kanban_origin
    # Trusted courier context is not an LLM argument. The owner adapter validates
    # its route; the DB validates/persists its immutable creation intent.
    origin_intent = kw.get("origin_intent")
    try:
        origin = (None if origin_intent is not None
                  else kanban_origin.resolve(args.get("origin_session")))
    except kanban_origin.OriginError as exc:
        return tool_error(f"{exc.code}: {exc}")
    human_action_now = _parse_bool_arg(args, "human_action_now")
    with _board(args.get("board")) as (kb, conn):
        _check(*create_ask_check(conn, args, str(assignee)))  # ask line, then its filing probe [daveey]
        args = _effective_board_args(args, origin_intent)
        # Never infer approval from prose or silently turn a human gate into
        # dispatchable work. Ask the creator to distinguish a future dependency
        # from a human action that can actually be taken while parents are open.
        if args.get("initial_status") == "blocked" and not human_action_now:
            open_parents = [pid for pid in (parents or [])
                            if (parent := kb.get_task(conn, pid)) and parent.status != "done"]
            _check(not open_parents,
                   "initial_status='blocked' with unfinished parents requires an immediate "
                   "human action. For 'after X, then Y', keep parents and omit initial_status "
                   "so the task waits in todo; the worker must request any required approval "
                   "when it becomes due, never assume approval. For an external/time condition, "
                   "use kanban_schedule with wake_check (clock_only for a pure time wait). "
                   "If a human really can act now despite the unfinished parents, set "
                   "human_action_now=true and explain that present action in body. Nothing changed.",
                   rejection="invalid_args")
        self_tid = (os.environ.get("HERMES_KANBAN_TASK")
                    if origin_intent is None and _is_dispatcher_owned_worker() else None)
        self_task = kb.get_task(conn, self_tid) if self_tid else None
        session_id = _create_session_id(args, origin_intent, self_task)
        if project_id is None and workspace_kind is None and workspace_path is None:
            if self_task is not None and self_task.project_id:
                project_id, project_source_task_id = self_task.project_id, self_task.id
        marker_placement, place_origin = None, {"session_id": session_id}
        # Placement on a synced board: ``runs_on`` labels, and a pin to an enrolled machine that advertises
        # pickup becomes ``runs_on=[machine:<name>]`` (created here, run there through the Board lease). Any
        # other pin, a routed gateway included, falls through to the gateway route below. [daveey]
        from hermes_cli import kanban_sync_runstate as _runstate  # runs_on rows need this host's sync [daveey]
        try:
            runs_on = _runstate.explicit_runs_on(conn, args.get("runs_on"), courier=origin_intent is not None)
        except ValueError as exc:
            sync_down = isinstance(exc, _runstate.SyncNotRunning)
            return tool_error(str(exc), **({"rejection": "sync_not_running"} if sync_down else {}))
        pinned_machine = None
        if runs_on is None and origin_intent is None:
            runs_on = _runstate.placement_runs_on(conn, args.get("placement"))
            if runs_on is not None:
                pinned_machine = str(args.get("placement")).strip()
                args = {**args, "placement": "local"}
        # [daveey] Refuse a second owner: open cards on every board/gateway, not this chat's links.
        refused, after_create = kanban_overlap.check(conn, args, courier=origin_intent is not None,
                                                     title=str(title).strip(), session_id=session_id,
                                                     self_tid=self_tid, parents=parents)
        if refused:
            return refused
        if origin_intent is None and origin is None:
            check_tool_create(args.get("requires"), workspace_kind=workspace_kind, project_id=project_id,
                              skills=skills, assignee=str(assignee))  # before placement routes it away [daveey]
            # Default placement: no local-only need -> the card runs on the routed gateway. [daveey]
            place_origin = _placement_origin(conn, self_tid, session_id)  # a worker's: its creator's chat
            remote, marker_placement = _place_remote(
                args, assignee=str(assignee), skills=skills, parents=parents, project_id=project_id, conn=conn,
                note_local_pin=pinned_machine is None, **place_origin)  # a machine pin was rewritten to "local"
            if remote is not None:
                return remote
            if marker_placement and marker_placement.get("runs_on") and runs_on is None:
                runs_on = marker_placement["runs_on"]
        elif origin_intent is None and args.get("placement"):
            # An origin-bound card is created here by design, so a pin to another gateway cannot be honoured.
            from hermes_cli import kanban_capability_registry as _reg
            from hermes_cli import kanban_placement as _kp
            _pin = _kp.explicit_foreign_pin(args, _reg.self_gateway())
            if _pin:
                return _placement_refused(_pin, "an origin-bound card is created on this gateway")
        marker_placement = _origin_enrollment(args, origin, marker_placement, assignee=assignee,
                                               skills=skills, project_id=project_id)
        try:
            new_tid = kb.create_task(
                conn, title=str(title).strip(), body=args.get("body"), assignee=str(assignee),
                parents=tuple(parents), tenant=(args.get("tenant") if origin_intent is not None
                                               else args.get("tenant") or os.environ.get("HERMES_TENANT")),
                priority=_opt_int(args.get("priority"), 0),
                workspace_kind=workspace_kind, workspace_path=workspace_path, project_id=project_id,
                # Board-project inheritance must read the board this call opened, not the
                # session's current board.
                board=args.get("board"),
                project_source_task_id=project_source_task_id, triage=triage,
                creator_task_id=self_tid,
                idempotency_key=args.get("idempotency_key"),
                max_runtime_seconds=_opt_int(args.get("max_runtime_seconds")), skills=skills,
                requires=args.get("requires"), runs_on=runs_on, max_retries=max_retries,
                placement_policy=(marker_placement or {}).get("placement_policy"),
                placement_checked=True,  # this front door already checked pins, origins and portability
                placement_audit=_creation_audit(args, assignee=str(assignee), skills=skills, project_id=project_id),
                require_declaration=origin_intent is None and declaration_required(),  # couriers exempt [daveey]
                model_override=model_override, provider_override=provider_override,
                goal_mode=goal_mode, goal_max_turns=_opt_int(args.get("goal_max_turns")),
                completion_contract=args.get("completion_contract"),
                initial_status=str(args.get("initial_status") or "running"),
                human_request=kb.redact_review_value(args.get("human_request")),
                created_by=_persisted_identity(), session_id=session_id,
                origin_intent=origin_intent, chat_origin=origin.ref if origin else args.get("origin_session"),
                # Reject a worktree path the dispatcher could never resolve now, to the
                # creator, instead of burning spawn retries on it later. [daveey]
                validate_workspace=True, validate_skills=True,
                insert_guard=after_create.guard)  # re-checks overlap under the write lock [daveey]
        except kanban_overlap.OverlapRefused as exc:  # landed under the write lock after the first check
            return exc.settle()
        landed = _fields(kb.get_task(conn, new_tid), _ca.CREATED_FIELDS)
        wait = [e for e in kb.list_events(conn, new_tid) if e.kind == "dependency_wait"]
        gate = {"gated": True, "gated_by": wait[-1].payload["parent"]} if wait else {"gated": False}
        extra = {}
        if origin_intent is not None:
            row = conn.execute(
                "SELECT binding_json,child_request_id FROM origin_creation_intents WHERE request_id=? AND task_id=?",
                (origin_intent["request_id"], new_tid),
            ).fetchone()
            extra = {"handoff_complete": False, "origin_creation": {
                "request_id": origin_intent["request_id"],
                "binding": json.loads(row["binding_json"]),
                "child_request_id": row["child_request_id"],
            }}
        if origin is not None:
            extra["origin_binding"] = kanban_origin.bind(conn, new_tid, origin, board=args.get("board"))
        extra.update(_marker_placement_fields(conn, new_tid, marker_placement, **place_origin))
        after_create(conn, new_tid)
        if origin_intent is None and args.get("requires"):
            # Placement preflight: declared gateway needs this gateway lacks are owned first. [daveey]
            from hermes_cli.kanban_capability_routing import preflight
            placed = preflight(conn, new_tid, board=args.get("board"))
            if placed:
                extra["capability_placement"] = placed
                landed = _fields(kb.get_task(conn, new_tid), _ca.CREATED_FIELDS)
        if runs_on is not None:
            # Say where the card runs: a pin rewritten to runs_on otherwise reads like an ignored pin,
            # and a caller that archives and re-creates it makes a duplicate. [daveey]
            extra["runs_on"] = runs_on
            if pinned_machine:
                extra["placement"] = _machine_placement(pinned_machine, runs_on)
                extra["note"] = (f"Created here as {new_tid}; it runs on {pinned_machine} via the board lease "
                                 f"(runs_on={runs_on}). The pin was honoured: do not re-create it.")
        # Every result says where the card runs (a caller that can't tell re-creates it or never learns it is stranded)
        extra.update(runs_where=_runs_where(runs_on, pinned_machine, marker_placement),  # + contract advisory [daveey]
                     **_contract_cli.create_warning_fields(kb.get_task(conn, new_tid)))
        if origin_intent is not None:
            # The courier is not a chat: the bound origin is subscribed from the binding in the same txn
            # (kanban_origin_creation._subscribe_origin), never the receiving process's ambient session. [daveey]
            return json.dumps({"ok": False, "created": True, "code": "origin_bind_pending",
                               "task_id": new_tid, **landed, **gate,
                               "subscribed": False, **extra})
        return _ok(task_id=new_tid, **landed, **gate,
                   subscribed=_maybe_auto_subscribe(conn, new_tid), **extra)


def _live_tui_session_key(session_key: str, profile: Optional[str]) -> str:
    """Re-resolve a TUI session key at subscribe time: the inherited ``HERMES_SESSION_KEY``
    can name a session already superseded by a compaction fork, and a subscription bound to
    the dead key silently drops every later completion notification (#110068). Maps the key
    to its continuation tip via the session store's lineage walk. Best-effort, fail-open:
    any error (or an unresolvable key) returns *session_key* unchanged."""
    db_path = None
    if profile and profile != "default":
        try:
            from pathlib import Path
            from hermes_cli.profiles import get_profile_dir, profile_exists
            if profile_exists(profile):
                db_path = Path(get_profile_dir(profile)) / "state.db"
        except Exception:
            db_path = None
    try:
        from hermes_state_registry import acquire, release_or_close
        db = acquire(db_path)
        try:
            return db.resolve_resume_session_id(session_key) or session_key
        finally:
            release_or_close(db)
    except Exception:
        return session_key


def _bound_session_var(name: str) -> Optional[str]:
    """The request-scoped ContextVar value of a ``HERMES_SESSION_*`` var, or None when it was never bound in this
    context. Unlike ``get_session_env`` it never falls back to ``os.environ``. [daveey]"""
    try:
        from gateway import session_context as _sc
        var = _sc._VAR_MAP.get(name)
        if var is None:
            return None
        value = var.get()
        return None if value is _sc._UNSET else str(value or "")
    except Exception:
        return None


def _tui_session_key() -> str:
    """The chat a TUI/Desktop subscription belongs to: the bound ContextVar key. Otherwise the process-global
    ``os.environ`` key (a CLI subprocess that inherited its chat's env), but never when it names a different
    chat than the session id bound in this context. In a multi-session serve process ``os.environ`` holds
    whichever chat was set up last, so trusting it subscribed another chat to this one's card.
    ``HERMES_SESSION_ID`` alone never subscribes (it is set for every CLI run). [daveey]"""
    key = _bound_session_var("HERMES_SESSION_KEY")
    if key:
        return key
    env_key = os.environ.get("HERMES_SESSION_KEY", "")
    sid = _bound_session_var("HERMES_SESSION_ID")
    if env_key and sid and env_key != sid:
        logger.info("kanban auto-subscribe: ignoring process HERMES_SESSION_KEY %s; this context runs %s",
                    env_key, sid)
        return ""
    return env_key


def _resolve_notify_target() -> Optional[dict[str, Any]]:
    """``kanban_db.add_notify_sub`` kwargs for the calling session, or None (CLI/cron/tests).
    Gateway sessions: ``HERMES_SESSION_PLATFORM``/``CHAT_ID`` ContextVars. TUI/desktop:
    those are cleared but the subprocess inherits ``HERMES_SESSION_KEY`` -> ``platform="tui"``
    for the TUI poller. ``HERMES_SESSION_ID`` is deliberately NOT a fallback: it is set for
    every CLI/ACP invocation and would auto-subscribe every CLI run."""
    from gateway.session_context import get_session_env as env
    platform, chat_id = env("HERMES_SESSION_PLATFORM", ""), env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        session_key = _tui_session_key()
        if not session_key:
            return None
        platform, chat_id = "tui", session_key
    chat_type = env("HERMES_SESSION_CHAT_TYPE", "") or None
    thread_id = env("HERMES_SESSION_THREAD_ID", "") or None
    message_id = env("HERMES_SESSION_MESSAGE_ID", "") or ""
    notifier_profile = env("HERMES_SESSION_PROFILE", "")
    if not notifier_profile:
        from hermes_cli.profiles import current_profile_name
        notifier_profile = current_profile_name("default")
    if platform == "tui":
        # The inherited key can be stale after a compaction fork (#110068): bind the
        # subscription to the live continuation tip, not the key the process started with.
        chat_id = _live_tui_session_key(chat_id, notifier_profile)
    delivery_metadata: dict[str, Any] = {
        k: v for k, v in (
            ("thread_id", thread_id), ("chat_type", chat_type),
            ("scope_id", env("HERMES_SESSION_SCOPE_ID", "")),
            ("parent_chat_id", env("HERMES_SESSION_PARENT_CHAT_ID", "")),
        ) if v}
    if (platform.lower() == "telegram" and thread_id
            and (chat_type or "").lower() in {"dm", "direct", "private"}):
        delivery_metadata["telegram_dm_topic_reply_fallback"] = True
        if str(thread_id) not in {"", "1"}:
            delivery_metadata["direct_messages_topic_id"] = str(thread_id)
        if message_id:
            delivery_metadata["telegram_reply_to_message_id"] = str(message_id)
    return dict(
        platform=platform, chat_id=chat_id, chat_type=chat_type, thread_id=thread_id,
        user_id=env("HERMES_SESSION_USER_ID", "") or None,
        user_id_alt=env("HERMES_SESSION_USER_ID_ALT", "") or None,
        notifier_profile=notifier_profile,
        delivery_mode="notify+wake" if platform != "tui" else None,
        delivery_metadata=delivery_metadata or None)


def _maybe_auto_subscribe(conn: Any, task_id: str, *, gate: str = "auto_subscribe_on_create",
                          gate_default: bool = True, reason: str = "create") -> bool:
    """Subscribe the calling session to completion/block events; True iff a row was
    written (surfaced as ``subscribed`` so an orchestrator can fall back to explicit
    ``kanban_notify-subscribe``). Gated by ``kanban.<gate>`` (``auto_subscribe_on_create``,
    default True). Failures are logged and swallowed: bookkeeping must never fail the tool."""
    try:
        if not cfg_get(load_config(), "kanban", gate, default=gate_default):
            return False
    except Exception:
        pass  # unreadable config keeps the user-friendly default (True)
    target = None
    try:
        target = _resolve_notify_target()
        if target is None:
            return False  # CLI / cron / test — no persistent channel
        from hermes_cli import kanban_db_notify as _kbn
        # Inheritance and explicit subscriptions already encode the delivery policy.
        # Auto-subscribe must not turn a passive destination into an agent wake.
        if any(sub["platform"] == target["platform"] and sub["chat_id"] == target["chat_id"]
               and (sub["thread_id"] or "") == (target["thread_id"] or "")
               for sub in _kbn.list_notify_subs(conn, task_id)):
            return True
        # ``reason`` decides whether the sub keeps the chat's sidebar row busy/red
        # (kanban_session_activity.COUNTED_SUB_REASONS): ``create`` does, ``touch`` doesn't. [daveey]
        if target["platform"] == "tui":
            target = {**target, "delivery_metadata": {**(target.get("delivery_metadata") or {}), "reason": reason}}
        _kbn.add_notify_sub(conn, task_id=task_id, **target)
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, target["platform"] if target else "", bool(target and target["chat_id"]))
        return False


def _maybe_subscribe_on_touch(conn: Any, task_id: str) -> bool:
    """A chat session that comments on, unblocks or links an existing card is
    shepherding it, so subscribe it like ``kanban_create`` does: it then wakes when
    the card completes or blocks. The sub is stamped ``reason: touch``, so it does NOT
    keep the chat's sidebar row busy or red (a one-off comment on someone else's
    scheduled soak or human block is not this chat's work); ``hermes kanban
    notify-subscribe`` marks an explicit shepherd that does count. Dispatcher workers
    (``HERMES_KANBAN_TASK`` set) never subscribe this way. Gated by
    ``kanban.auto_subscribe_on_touch`` (default False). [daveey]"""
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _maybe_auto_subscribe(conn, task_id, gate="auto_subscribe_on_touch", gate_default=False,
                                 reason="touch")


@_kanban_handler("kanban_archive")
def _handle_archive(args: dict, **kw) -> str:
    """Archive an unclaimed or done card this worker's task created, or any card for an orchestrator."""
    _reject_delegated_child_mutation("kanban_archive")  # may relay to the card's home gateway
    tid = args.get("task_id")
    _check(tid, "task_id is required")
    tid = str(tid)
    with _board(args.get("board")) as (_kb, conn):
        # [daveey] Scope rule, stop request for a card leased elsewhere, relayed owner card forwarded home.
        from hermes_cli import kanban_foreign_card as _kfc
        from hermes_cli.kanban_owner_archive_relay import worker_archive
        try:
            refusal, out = worker_archive(conn, tid, run_id_fn=_worker_run_id)
        except _kfc.ForeignCardError as exc:
            raise _Reject(str(exc), _kfc.REJECTION) from None
        if refusal:
            raise _Reject(refusal)
        _check(out, f"cannot archive {tid}")
        return _ok(task_id=tid, **out)


@_kanban_handler("kanban_origin_adopt")
def _handle_origin_adopt(args: dict, **kw) -> str:
    _reject_delegated_child_mutation("kanban_origin_adopt")
    intent = kw.get("origin_intent")
    if intent is None:
        # Never a dead end: name the supported CLI path (hermes-hub adopt-task) filled in from args. [daveey]
        from hermes_cli.kanban_origin import adopt_next_step
        try:
            hint = adopt_next_step(str(args.get("task_id") or "<task-id>"), str(args.get("board") or "default"))
        except Exception:
            logger.debug("kanban_origin_adopt next_step failed", exc_info=True)
            hint = {}
        message = hint.pop("message", "")
        return tool_error(f"authenticated origin courier context required. {message}".strip().rstrip("."),
                          code="origin_context_required", handoff_complete=False, **hint)
    from hermes_cli.kanban_origin import OriginError
    from hermes_cli.kanban_origin_creation import adopt_with_origin
    if (not isinstance(intent, dict) or not isinstance(intent.get("owner"), dict)
            or intent["owner"].get("task_id") != args.get("task_id")
            or intent["owner"].get("board") != args.get("board")):
        raise OriginError("origin_adoption_invalid", "adoption route does not match task arguments")
    with _board(args["board"]) as (_, conn):
        tid = adopt_with_origin(conn, intent, board=args["board"])
        row = conn.execute("SELECT binding_json,child_request_id FROM origin_creation_intents WHERE request_id=?",
                           (intent["request_id"],)).fetchone()
        return json.dumps({"ok": False, "created": False, "adopted": True, "task_id": tid,
                           "code": "origin_bind_pending", "handoff_complete": False,
                           "origin_creation": {"request_id": intent["request_id"],
                                               "binding": json.loads(row["binding_json"]),
                                               "child_request_id": row["child_request_id"]}})


# --- Registration (order preserved: it is the order tools appear in the schema) ---

# kanban_list routes the board and is hidden from task workers. kanban_unblock's
# gate lives in kanban_tools_self_heal (orchestrators, plus a self-heal requeue grant).
_ORCHESTRATOR_TOOLS = frozenset({"kanban_list"})
from tools import kanban_tools_graph as _graph
_handle_link = _kanban_handler("kanban_link")(_graph.handle_link)
_handle_unlink = _kanban_handler("kanban_unlink")(_graph.handle_unlink)
_handle_supervisor_request = _kanban_handler("kanban_supervisor_request")(_graph.handle_supervisor_request)
from tools.kanban_tools_self_heal import check_unblock as _check_kanban_unblock_fn
from tools.kanban_tools_self_heal import handle_unblock as _handle_unblock_fn, granted_board as _granted_board
_check_kanban_unblock = no_cache_check_fn(_check_kanban_unblock_fn)
_handle_unblock = _kanban_handler("kanban_unblock")(_handle_unblock_fn)
_TOOLS = (
    ("kanban_show", KANBAN_SHOW_SCHEMA, _handle_show, "📋"),
    ("kanban_list", KANBAN_LIST_SCHEMA, _handle_list, "📋"),
    ("kanban_complete", KANBAN_COMPLETE_SCHEMA, _handle_complete, "✔"),
    ("kanban_block", KANBAN_BLOCK_SCHEMA, _handle_block, "⏸"),
    ("kanban_schedule", KANBAN_SCHEDULE_SCHEMA, _handle_schedule, "⏰"),
    ("kanban_continue", KANBAN_CONTINUE_SCHEMA, _handle_continue, "⏭"),
    ("kanban_move", KANBAN_MOVE_SCHEMA, _handle_move, "🚚"),
    ("kanban_request_review", KANBAN_REQUEST_REVIEW_SCHEMA, _handle_request_review, "👀"),
    ("kanban_request_changes", KANBAN_REQUEST_CHANGES_SCHEMA, _handle_request_changes, "↩"),
    ("kanban_heartbeat", KANBAN_HEARTBEAT_SCHEMA, _handle_heartbeat, "💓"),
    ("kanban_comment", KANBAN_COMMENT_SCHEMA, _handle_comment, "💬"),
    ("kanban_attach", KANBAN_ATTACH_SCHEMA, _handle_attach, "📎"),
    ("kanban_attach_url", KANBAN_ATTACH_URL_SCHEMA, _handle_attach_url, "📎"),
    ("kanban_attachments", KANBAN_ATTACHMENTS_SCHEMA, _handle_attachments, "📎"),
    ("kanban_create", KANBAN_CREATE_SCHEMA, _handle_create, "➕"),
    ("kanban_origin_adopt", KANBAN_ORIGIN_ADOPT_SCHEMA, _handle_origin_adopt, "🔗"),
    ("kanban_unblock", KANBAN_UNBLOCK_SCHEMA, _handle_unblock, "▶"),
    ("kanban_link", KANBAN_LINK_SCHEMA, _handle_link, "🔗"),
    ("kanban_unlink", KANBAN_UNLINK_SCHEMA, _handle_unlink, "✂"),
    ("kanban_supervisor_request", KANBAN_SUPERVISOR_REQUEST_SCHEMA, _handle_supervisor_request, "🛂"),
    ("kanban_archive", KANBAN_ARCHIVE_SCHEMA, _handle_archive, "🗄"))

for _name, _sch, _handler, _emoji in _TOOLS:
    if _name == "kanban_unblock":
        _gate = _check_kanban_unblock
    elif _name in _ORCHESTRATOR_TOOLS:
        _gate = _check_kanban_orchestrator_mode
    else:
        _gate = _check_kanban_mode
    registry.register(name=_name, toolset="kanban", schema=_sch, handler=_handler, emoji=_emoji,
                      check_fn=_gate)
