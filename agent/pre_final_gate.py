"""``pre_final_gate`` plugin hook: hold or amend a final text answer before the user sees it. [daveey]

Fires on every text stop (after verify-on-stop, ``pre_verify`` and the kanban guard). Callbacks get
``session_id, platform, model, final_response, turn_messages, attempt`` (keyword args) and return:

- ``None`` (or anything else) → deliver unchanged.
- ``{"hold": True, "nudge": str}`` → the candidate is NOT emitted, NOT streamed (see below) and NOT
  persisted; it is kept only as the budget-exhaustion fallback (``pending_verification_response``).
  The held row plus the nudge (a synthetic user row) stay in the in-memory turn so the model sees
  them, and are stripped from the returned/live history at turn end. The turn continues.
- ``{"append": str}`` → deliver with ``"\\n\\n" + text`` appended.

``attempt`` counts holds already issued this turn. Once it reaches ``agent.pre_final_gate_max``
(default 2) a hold is downgraded to deliver (its ``append``, if any, is still applied). A callback
that raises or exceeds ``agent.pre_final_gate_timeout`` seconds (default 30) is logged and ignored.

Callback attributes (set on the function before ``ctx.register_hook``):

- ``needs_buffer = True``: assistant text deltas are buffered for the whole turn while this hook is
  eligible, and released only when a response turns out to carry tool calls (commentary), when the
  gate delivers, or at turn end. A held answer is discarded from the buffer, so it never reaches
  the screen. Neither Desktop nor CLI/gateway streams can retract text already shown, hence the
  buffer: without it, a held answer that already streamed stays visible.
- ``include_background = True``: also run in kanban workers, delegate_task subagents and cron jobs
  (skipped there by default).
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
from dataclasses import dataclass
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
    return (
        isinstance(msg, dict) and msg.get("role") == "user"
        and not any(k.startswith("_") and k.endswith(("_synthetic", "_nudge")) and msg.get(k) for k in msg)
    )


def turn_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """This turn's rows: from the last real (non-synthetic) user message to the end."""
    for idx in range(len(messages) - 1, -1, -1):
        if _is_real_user_row(messages[idx]):
            return list(messages[idx:])
    return list(messages)


@dataclass
class GateDecision:
    hold: bool = False
    nudge: str = ""
    append: str = ""


def _call(cb: Callable, kwargs: dict[str, Any], limit: float) -> Any:
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="pre-final-gate")
    try:
        return pool.submit(cb, **kwargs).result(timeout=limit)
    finally:
        pool.shutdown(wait=False)


def evaluate(agent: Any, final_response: Any, messages: list[dict[str, Any]], attempt: int) -> GateDecision:
    """Run eligible callbacks in registration order. First hold wins (while under the limit);
    appends from the others accumulate. Never raises."""
    decision = GateDecision()
    if not isinstance(final_response, str) or not final_response.strip():
        return decision
    callbacks = eligible_callbacks(agent)
    if not callbacks:
        return decision
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
            logger.warning("pre_final_gate hook %s timed out after %.1fs; delivering", name, limit)
            continue
        except Exception:
            logger.warning("pre_final_gate hook %s raised; delivering unchanged", name, exc_info=True)
            continue
        if not isinstance(result, dict):
            continue
        nudge = result.get("nudge")
        if result.get("hold") and isinstance(nudge, str) and nudge.strip():
            if can_hold:
                return GateDecision(hold=True, nudge=nudge.strip())
            logger.info("pre_final_gate hold from %s ignored: limit reached (attempt %d)", name, attempt)
        extra = result.get("append")
        if isinstance(extra, str) and extra.strip():
            appends.append(extra.strip())
    decision.append = "\n\n".join(appends)
    return decision


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "FLAG",
    "HOOK",
    "GateDecision",
    "eligible_callbacks",
    "evaluate",
    "is_background_agent",
    "max_attempts",
    "needs_buffer",
    "turn_messages",
]
