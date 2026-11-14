"""``pre_final_gate`` plugin hook: hold or amend a final text answer before the user sees it. [daveey]

Fires on every text stop. Buffered gates run before verification interims can expose
a candidate; unbuffered legacy gates run after verification and the kanban guard. Callbacks get
``session_id, platform, model, final_response, turn_messages, attempt`` (keyword args) and return:

- ``None`` (or anything else) → deliver unchanged.
- ``{"hold": True, "nudge": str}`` → the candidate is NOT emitted, NOT streamed (see below) and NOT
  persisted; it is kept only as the budget-exhaustion fallback (``pending_verification_response``).
  The held row plus the nudge (a synthetic user row) stay in the in-memory turn so the model sees
  them, and are stripped from the returned/live history at turn end. The turn continues. When the
  held draft did not stream to the screen, core prefixes the nudge with ``HELD_NOTICE`` ("your
  draft above was NOT shown ... include its full content"), whatever the hook wrote.
- ``{"append": str}`` → deliver with ``"\\n\\n" + text`` appended.
- ``{"replace": str}`` → replace the entire candidate with nonempty safe text, independent of
  hold budget. Clears native replay carriers and the rejected stream buffer; other completion
  guards still run against the safe candidate. Requires ``needs_buffer`` to prevent prior streaming.
  All callbacks inspect the original candidate. Replacements take precedence over ordinary holds
  and appends regardless of registration order; conflicting replacements yield neutral withheld
  text. An unresolved rejection or unavailable owning recheck still fails closed.

A hold may also carry ``unresolved_fallback={"goal": str, "evidence_refs": list[str]}``.
Only these two keys are allowed: a nonblank goal (at most 2000 characters), and 1–32
nonblank references (at most 500 characters each). Invalid values retain legacy behavior.
The plugin verifies the evidence; core validates shape, not the underlying goal or references.
Use ``needs_buffer=True`` to prevent rejected text from streaming before evaluation.
This opt-in replaces the rejected answer with a runtime-authored unresolved-goal boundary
on budget exhaustion, hold-cap exhaustion, or unavailable continuation/recheck. It does not
grant authority, create an owner, extend budgets, or claim remediation. A successful recheck
clears the pending fallback; Stop/cancel always takes precedence. The exact holding callback
owns that recheck and runs first while pending. Removal or replacement on reload is an
unavailable recheck, not acceptance by another callback.

``attempt`` counts holds already issued this turn. Once it reaches ``agent.pre_final_gate_max``
(default 2) a hold is downgraded to deliver (its ``append``, if any, is still applied). A callback
that raises or exceeds ``agent.pre_final_gate_timeout`` seconds (default 30) is logged and ignored.

Callback attributes (set on the function before ``ctx.register_hook``):

- ``needs_buffer = True``: assistant text deltas are buffered for the whole turn while this hook is
  eligible, and released only when a response turns out to carry tool calls (commentary), when the
  gate delivers, or at turn end. A held answer is discarded from the buffer, so it never reaches
  the screen. Neither Desktop nor CLI/gateway streams can retract text already shown, hence the
  buffer: without it, a held answer that already streamed stays visible.
- ``needs_buffer_for_session(session_id=...)``: optional synchronous predicate that overrides
  the legacy ``needs_buffer`` flag for this session. Sampled once after all ``pre_llm_call`` hooks,
  before inference, so hooks may arm per-turn state without buffering unrelated sessions.
  Predicate errors are logged and fall back to the legacy non-callable flag; scope opt-ins still apply.
- ``accepts_hold_budget = True``: opt in to the ``hold_budget`` keyword, the configured total
  holds allowed this turn (including zero). A callback can replace a rejected draft instead of
  returning an unusable hold at exhaustion. Strict legacy signatures receive no new keyword.
- ``assess_before_holds = True``: assess before ordinary gates so the disposition guard's
  nudge wins. Use for buffered guards that provide an unresolved fallback. Pending callback
  ownership still takes precedence; registration order is unchanged when no callback opts in.
- ``include_background = True``: also run in kanban workers, delegate_task subagents and cron jobs
  (skipped there by default).
- ``include_forks = True``: also run on detached fork turns (background review, side questions:
  ``_turn_origin`` / ``_persist_disabled``), skipped there by default. A fork shares the parent's
  session_id and platform, but it is not a reply in the conversation: a review fork's text is never
  shown, and a ``/btw`` answer is a tool-less side note outside the transcript, so a hold's nudge
  (file a card, publish an ask) cannot be acted on and only burns the fork's few iterations, while
  per-session gate state would mix with the parent's. Same rule as ``pre_verify`` and
  ``post_llm_call``. A fork that is also a background agent needs both opt-ins.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

logger = logging.getLogger("agent.conversation_loop")

HOOK = "pre_final_gate"
FLAG = "_pre_final_gate_synthetic"
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_TIMEOUT_S = 30.0
# Prefixed by core to every hold nudge whose draft never reached the screen. The held row stays in the model's
# context, so without this line a model took its held explanation as delivered and sent only "The explanation and
# recommendations are complete." (hub 20261028_071229_45f849). Hooks cannot be trusted to say it themselves.
HELD_NOTICE = ("[runtime] Your draft above was NOT shown to the user. Your next reply replaces it: include its "
               "full content; don't refer to it as 'above'.")


def held_nudge(nudge: str, shown: bool) -> str:
    """The nudge row for a hold: ``HELD_NOTICE`` first unless the held draft already streamed to the screen."""
    return nudge if shown else f"{HELD_NOTICE}\n\n{nudge}"

# Explicit plugin compatibility handshake. Publish only with the matching turn
# consumers: replacement, exhausted-budget kwargs and post-transform emission.
PRE_FINAL_GATE_CAPABILITIES = {
    "version": 1,
    "replace": True,
    "accepts_hold_budget": True,
    "needs_buffer": True,
    "post_transform_release": True,
}


def _agent_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("agent")
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def max_attempts() -> int:
    """``agent.pre_final_gate_max``: holds allowed per turn before the gate delivers (>= 0)."""
    try:
        return max(0, int(_agent_cfg().get("pre_final_gate_max", DEFAULT_MAX_ATTEMPTS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


def timeout_s() -> float:
    try:
        return max(0.1, float(_agent_cfg().get("pre_final_gate_timeout", DEFAULT_TIMEOUT_S)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def is_background_agent(agent: Any) -> bool:
    """Kanban worker, delegate_task subagent or cron run."""
    if (getattr(agent, "platform", "") or "") == "cron":
        return True
    if (getattr(agent, "_delegate_depth", 0) or 0) > 0:
        return True
    if os.environ.get("HERMES_KANBAN_TASK"):
        try:
            from agent.delegation_context import is_dispatcher_owned_worker_context
        except Exception:
            return True
        try:
            return bool(is_dispatcher_owned_worker_context())
        except Exception:
            return True
    return False


def is_fork_turn(agent: Any) -> bool:
    """Detached fork (background review, ``/btw`` side question): not a reply in the conversation."""
    return bool(getattr(agent, "_turn_origin", None) or getattr(agent, "_persist_disabled", False))


def eligible_callbacks(agent: Any) -> list[Callable]:
    """Registered ``pre_final_gate`` callbacks that apply to this agent's context."""
    try:
        from hermes_cli.lifecycle import has_hook

        if not has_hook(HOOK):
            return []
        from hermes_cli.plugins import iter_hook_callbacks

        callbacks = list(iter_hook_callbacks(HOOK))
    except Exception:
        logger.debug("pre_final_gate lookup failed", exc_info=True)
        return []
    if callbacks and is_background_agent(agent):
        callbacks = [cb for cb in callbacks if getattr(cb, "include_background", False)]
    if callbacks and is_fork_turn(agent):
        callbacks = [cb for cb in callbacks if getattr(cb, "include_forks", False)]
    return callbacks


def needs_buffer(agent: Any) -> bool:
    """Sample eligible gates after turn-start hooks have armed their session state."""
    for cb in eligible_callbacks(agent):
        predicate = getattr(cb, "needs_buffer_for_session", None)
        if callable(predicate):
            try:
                if predicate(session_id=getattr(agent, "session_id", None) or ""):
                    return True
                continue
            except Exception:
                logger.warning("pre_final_gate buffer predicate failed; using legacy flag", exc_info=True)
        legacy = getattr(cb, "needs_buffer", False)
        if not callable(legacy) and bool(legacy):
            return True
    return False


def _is_real_user_row(msg: Any) -> bool:
    from agent.message_metadata import PERSISTENCE_ONLY_MESSAGE_FIELDS
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    metadata = msg.get("display_metadata") or {}
    if isinstance(metadata, str):
        import json
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return False
    # Steering is part of the active turn; UI receipts and runtime rows cannot
    # cut away a human restriction that preceded them.
    return (
        not msg.get("display_kind") and not msg.get("observed")
        and not (isinstance(metadata, dict) and metadata.get("_queued_prompt") is True)
        and not any(k.startswith("_") and k not in PERSISTENCE_ONLY_MESSAGE_FIELDS
                    and k != "_db_persisted" and msg[k] for k in msg)
    )


def turn_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """From the last ordinary human turn, including mid-turn steering and tools."""
    for idx in range(len(messages) - 1, -1, -1):
        if _is_real_user_row(messages[idx]):
            return list(messages[idx:])
    return list(messages)


@dataclass(frozen=True)
class UnresolvedFallback:
    """Validated plugin evidence; never a candidate answer or an ownership claim."""

    goal: str
    evidence_refs: tuple[str, ...]
    # Runtime identity, not plugin-supplied ownership or a serializable task owner.
    recheck_callback: Optional[Callable] = field(default=None, repr=False, compare=False)

    def render(self, boundary: str) -> str:
        return (
            f"Unresolved goal: {self.goal}\n"
            f"Runtime boundary: {boundary}. No further work is running in this turn.\n"
            f"Evidence references: {', '.join(self.evidence_refs)}\n"
            "The requested outcome has not been verified; diagnosis is not completion."
        )


def _unresolved_fallback(value: Any) -> Optional[UnresolvedFallback]:
    if not isinstance(value, dict) or set(value) != {"goal", "evidence_refs"}:
        return None
    goal, refs = value["goal"], value["evidence_refs"]
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
        return None
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        return None
    if any(not isinstance(ref, str) or not ref.strip() or len(ref) > 500 for ref in refs):
        return None
    return UnresolvedFallback(goal.strip(), tuple(ref.strip() for ref in refs))


@dataclass
class GateDecision:
    hold: bool = False
    nudge: str = ""
    append: str = ""
    replacement: Optional[str] = None
    unresolved_fallback: Optional[UnresolvedFallback] = None
    boundary: str = ""
    recheck_accepted: bool = False


def _call(cb: Callable, kwargs: dict[str, Any], limit: float) -> Any:
    from contextvars import copy_context

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pre-final-gate")
    try:
        return pool.submit(copy_context().run, cb, **kwargs).result(timeout=limit)
    finally:
        pool.shutdown(wait=False)


_FAILED = object()


def _invoke(cb: Callable, kwargs: dict[str, Any], budget: int, limit: float) -> Any:
    """Run one gate callback; a timeout or exception yields ``_FAILED``."""
    name = getattr(cb, "__qualname__", repr(cb))
    call_kwargs = dict(kwargs)
    if getattr(cb, "accepts_hold_budget", False):
        call_kwargs["hold_budget"] = budget
    try:
        return _call(cb, call_kwargs, limit)
    except concurrent.futures.TimeoutError:
        logger.warning("pre_final_gate hook %s timed out after %.1fs", name, limit)
    except Exception as exc:
        from tools.interpreter_shutdown import interpreter_shutting_down

        if isinstance(exc, RuntimeError) and interpreter_shutting_down(exc):
            # The process is exiting (Desktop quit, update, restart) while a turn finishes: not a
            # gate failure. One quiet line, no traceback; fail open exactly as for any other error.
            logger.info("pre_final_gate hook %s skipped: %s (process exiting)", name, exc)
        else:
            logger.warning("pre_final_gate hook %s raised", name, exc_info=True)
    return _FAILED


def _absorb_hold(
    decision: GateDecision, result: dict, cb: Callable, pending: Any, can_hold: bool, name: str,
    attempt: int,
) -> bool:
    """Fold a hold result into ``decision``; True when the callback's result is consumed."""
    nudge = result.get("nudge")
    if not (result.get("hold") and isinstance(nudge, str) and nudge.strip()):
        return False
    fallback = _unresolved_fallback(result.get("unresolved_fallback"))
    if fallback is not None:
        fallback = replace(fallback, recheck_callback=cb)
    if fallback is None and isinstance(pending, UnresolvedFallback):
        fallback = pending
    if can_hold:
        if not decision.hold:
            decision.hold = True
            decision.nudge = nudge.strip()
        if decision.unresolved_fallback is None:
            decision.unresolved_fallback = fallback
        return True
    if fallback is not None:
        if decision.unresolved_fallback is None:
            decision.unresolved_fallback = fallback
            decision.boundary = "pre-final hold limit reached"
        return True
    logger.info("pre_final_gate hold from %s ignored: limit reached (attempt %d)", name, attempt)
    return False


def evaluate(
    agent: Any, final_response: Any, messages: list[dict[str, Any]], attempt: int,
    pending: Any = None,
) -> GateDecision:
    """Recheck the pending owner first, then disposition assessors, then ordinary gates.
    Inspect the original candidate throughout. Replacement consensus wins over
    ordinary holds, but cannot erase an unresolved owner's rejection or failed recheck.
    Otherwise the first hold wins; appends accumulate.
    """
    decision = GateDecision()
    if not isinstance(final_response, str) or not final_response.strip():
        return decision
    callbacks = eligible_callbacks(agent)
    if not callbacks:
        return _unavailable(pending)
    callbacks.sort(key=lambda cb: not bool(getattr(cb, "assess_before_holds", False)))
    owner = pending.recheck_callback if isinstance(pending, UnresolvedFallback) else None
    if owner is not None:
        if not any(cb is owner for cb in callbacks):
            return _unavailable(pending)
        # Another gate cannot skip the owning recheck. Reload is not acceptance.
        callbacks = [owner, *(cb for cb in callbacks if cb is not owner)]
    check_failed = False
    budget = max_attempts()
    limit, can_hold, appends = timeout_s(), attempt < budget, []
    kwargs = dict(
        session_id=getattr(agent, "session_id", None) or "",
        platform=getattr(agent, "platform", "") or "",
        model=getattr(agent, "model", "") or "",
        final_response=final_response,
        turn_messages=turn_messages(messages),
        attempt=attempt,
    )
    replacements: list[str] = []
    for cb in callbacks:
        name = getattr(cb, "__qualname__", repr(cb))
        result = _invoke(cb, kwargs, budget, limit)
        if result is _FAILED:
            if cb is owner:
                return _unavailable(pending)
            check_failed = True
            continue
        if cb is owner and not (isinstance(result, dict) and result.get("hold")):
            pending = None
            decision.recheck_accepted = True
        if not isinstance(result, dict):
            continue
        replacement = result.get("replace")
        if isinstance(replacement, str) and replacement.strip():
            replacements.append(replacement.strip())
            continue
        if _absorb_hold(decision, result, cb, pending, can_hold, name, attempt):
            continue
        extra = result.get("append")
        if isinstance(extra, str) and extra.strip():
            appends.append(extra.strip())
    if check_failed and isinstance(pending, UnresolvedFallback):
        return _unavailable(pending)
    # A different callback cannot turn a still-unresolved goal into completion.
    if decision.unresolved_fallback is not None:
        return decision
    if replacements:
        # Every validator inspects the same candidate. Do not let registration
        # order choose between contradictory replacements or revive rejected
        # text through an append. Completion guards still inspect the outcome.
        replacement = replacements[0] if len(set(replacements)) == 1 else (
            "Response withheld because validation gates disagreed."
        )
        return GateDecision(replacement=replacement, recheck_accepted=decision.recheck_accepted)
    decision.append = "\n\n".join(appends)
    return decision


def _unavailable(pending: Any) -> GateDecision:
    if isinstance(pending, UnresolvedFallback):
        return GateDecision(unresolved_fallback=pending, boundary="continuation unavailable")
    return GateDecision()


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "FLAG",
    "HELD_NOTICE",
    "HOOK",
    "GateDecision",
    "UnresolvedFallback",
    "eligible_callbacks",
    "evaluate",
    "held_nudge",
    "is_background_agent",
    "max_attempts",
    "needs_buffer",
    "turn_messages",
]
