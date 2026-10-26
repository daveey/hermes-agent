"""``pre_final_gate`` plugin hook: hold or amend a final text answer before the user sees it. [daveey]

Fires on every text stop. Buffered gates run before verification interims can expose
a candidate; unbuffered legacy gates run after verification and the kanban guard. Callbacks get
``session_id, platform, model, final_response, turn_messages, attempt`` (keyword args) and return:

- ``None`` (or anything else) → deliver unchanged.
- ``{"hold": True, "nudge": str}`` → the candidate is NOT emitted, NOT streamed (see below) and NOT
  persisted; it is kept only as the budget-exhaustion fallback (``pending_verification_response``).
  The held row plus the nudge (a synthetic user row) stay in the in-memory turn so the model sees
  them, and are stripped from the returned/live history at turn end. The turn continues.
- ``{"append": str}`` → deliver with ``"\\n\\n" + text`` appended.

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
- ``assess_before_holds = True``: run before ordinary gates can short-circuit the initial
  assessment. Use for buffered disposition guards that provide an unresolved fallback.
  Pending callback ownership still takes precedence; legacy registration order is unchanged
  when no callback opts in.
- ``include_background = True``: also run in kanban workers, delegate_task subagents and cron jobs
  (skipped there by default).
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
    return callbacks


def needs_buffer(agent: Any) -> bool:
    return any(getattr(cb, "needs_buffer", False) for cb in eligible_callbacks(agent))


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


def evaluate(
    agent: Any, final_response: Any, messages: list[dict[str, Any]], attempt: int,
    pending: Any = None,
) -> GateDecision:
    """Recheck the pending owner first, then disposition assessors, then ordinary gates.
    First hold wins (while under the limit); appends accumulate. Never raises."""
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
    limit, can_hold, appends = timeout_s(), attempt < max_attempts(), []
    kwargs = dict(
        session_id=getattr(agent, "session_id", None) or "",
        platform=getattr(agent, "platform", "") or "",
        model=getattr(agent, "model", "") or "",
        final_response=final_response,
        turn_messages=turn_messages(messages),
        attempt=attempt,
    )
    for cb in callbacks:
        name = getattr(cb, "__qualname__", repr(cb))
        try:
            result = _call(cb, kwargs, limit)
        except concurrent.futures.TimeoutError:
            logger.warning("pre_final_gate hook %s timed out after %.1fs", name, limit)
            if cb is owner:
                return _unavailable(pending)
            check_failed = True
            continue
        except Exception:
            logger.warning("pre_final_gate hook %s raised", name, exc_info=True)
            if cb is owner:
                return _unavailable(pending)
            check_failed = True
            continue
        if cb is owner and not (isinstance(result, dict) and result.get("hold")):
            pending = None
            decision.recheck_accepted = True
        if not isinstance(result, dict):
            continue
        nudge = result.get("nudge")
        if result.get("hold") and isinstance(nudge, str) and nudge.strip():
            fallback = _unresolved_fallback(result.get("unresolved_fallback"))
            if fallback is not None:
                fallback = replace(fallback, recheck_callback=cb)
            if fallback is None and isinstance(pending, UnresolvedFallback):
                fallback = pending
            if can_hold:
                return GateDecision(
                    hold=True, nudge=nudge.strip(),
                    unresolved_fallback=fallback,
                    recheck_accepted=decision.recheck_accepted,
                )
            if fallback is not None:
                return GateDecision(unresolved_fallback=fallback, boundary="pre-final hold limit reached")
            logger.info("pre_final_gate hold from %s ignored: limit reached (attempt %d)", name, attempt)
        extra = result.get("append")
        if isinstance(extra, str) and extra.strip():
            appends.append(extra.strip())
    if check_failed and isinstance(pending, UnresolvedFallback):
        return _unavailable(pending)
    decision.append = "\n\n".join(appends)
    return decision


def _unavailable(pending: Any) -> GateDecision:
    if isinstance(pending, UnresolvedFallback):
        return GateDecision(unresolved_fallback=pending, boundary="continuation unavailable")
    return GateDecision()


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "FLAG",
    "HOOK",
    "GateDecision",
    "UnresolvedFallback",
    "eligible_callbacks",
    "evaluate",
    "is_background_agent",
    "max_attempts",
    "needs_buffer",
    "turn_messages",
]
