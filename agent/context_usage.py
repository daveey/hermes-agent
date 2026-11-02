"""Per-provider-response context accounting for one ``run_conversation`` turn.

Exported on the turn result as ``context_usage``::

    {"version": 1, "complete": bool,
     "requests": [{"provider": str, "model": str, "input_tokens": int, "input_limit": int | None}]}

One entry per main-loop provider response, captured in ``agent/turn_usage.py`` from the
response's own (pre-MoA-fold) usage under the runtime identity that actually answered. This
is distinct from the billing totals (which sum every call, advisors included) and never
touches them. ``complete`` is False when any response this turn received could not be
measured individually: no/zero usage, a truncation or refusal diverted before measurement,
an adapter that aggregates several internal model calls into one usage figure (MoA advisors,
codex app-server, multi-hop long decode, provider-executed tools), an unresolvable or wire-unsafe
identity (over 80 chars or containing CR/LF/NUL, the ``served_runtime`` bound), or more than ``MAX_CONTEXT_REQUESTS`` responses. ``input_limit`` is the grounded runtime input
window (context window minus the output reservation), never the compression trigger, and
``None`` when the window is a guess or belongs to another runtime. A response whose request used
provider-executed tools gets no entry at all (its usage may be an aggregate); billing still sums it.

State lives on the agent instance (one turn at a time per agent: the turn lease), so
concurrent agents, forks and subagents never share an accumulator.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONTEXT_USAGE_VERSION = 1
MAX_CONTEXT_REQUESTS = 128
_ATTR = "_turn_context_usage"
# Virtual or unresolved provider labels are never an effective identity.
_NON_IDENTITY_PROVIDERS = frozenset({"", "auto", "moa"})
# Same bound the API server applies to ``served_runtime`` (``_clean_runtime_id(max_len=80)``).
MAX_RUNTIME_ID_LEN = 80
_UNSAFE_RUNTIME_ID_CHARS = ("\r", "\n", "\x00")


def is_wire_safe_runtime_id(value: Any) -> bool:
    """True for a non-empty, already-stripped id of at most 80 chars with no CR, LF or NUL."""
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= MAX_RUNTIME_ID_LEN and not any(c in value for c in _UNSAFE_RUNTIME_ID_CHARS))


class TurnContextUsage:
    """Accumulator for one turn. Thread-safe; ``snapshot`` returns plain JSON types."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []
        self._complete = True

    def add(self, provider: str, model: str, input_tokens: int, input_limit: Optional[int]) -> None:
        with self._lock:
            if len(self._requests) >= MAX_CONTEXT_REQUESTS:
                self._complete = False
                return
            self._requests.append({"provider": provider, "model": model,
                                   "input_tokens": int(input_tokens), "input_limit": input_limit})

    def mark_incomplete(self, reason: str) -> None:
        with self._lock:
            self._complete = False
        logger.debug("context usage incomplete: %s", reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"version": CONTEXT_USAGE_VERSION, "complete": self._complete,
                    "requests": [dict(r) for r in self._requests]}


def begin_turn_context_usage(agent: Any) -> TurnContextUsage:
    """Install a fresh accumulator for the turn that is starting on ``agent``."""
    acc = TurnContextUsage()
    setattr(agent, _ATTR, acc)
    return acc


def active_turn_context_usage(agent: Any) -> Optional[TurnContextUsage]:
    """The agent's live accumulator, or None outside a turn (forks, side questions, stubs)."""
    acc = getattr(agent, _ATTR, None)
    return acc if isinstance(acc, TurnContextUsage) else None


def export_turn_context_usage(agent: Any) -> Optional[dict[str, Any]]:
    acc = active_turn_context_usage(agent)
    return acc.snapshot() if acc is not None else None


def mark_turn_context_incomplete(agent: Any, reason: str) -> None:
    acc = active_turn_context_usage(agent)
    if acc is not None:
        acc.mark_incomplete(reason)


def _effective_identity(agent: Any, moa_slot: Any) -> Optional[tuple[str, str]]:
    """``(provider, model)`` of the runtime that served the response, or None when unknown.
    MoA answers come from the aggregator slot; class selectors and virtual providers never count."""
    from agent.model_routing import is_class_selector

    if str(getattr(agent, "provider", "") or "").strip().lower() == "moa":
        if not isinstance(moa_slot, dict):
            return None
        provider, model = moa_slot.get("provider"), moa_slot.get("model")
    else:
        provider, model = getattr(agent, "provider", None), getattr(agent, "model", None)
    if not (isinstance(provider, str) and isinstance(model, str)):
        return None
    provider, model = provider.strip(), model.strip()
    if provider.lower() in _NON_IDENTITY_PROVIDERS or not model or is_class_selector(model):
        return None
    if not (is_wire_safe_runtime_id(provider) and is_wire_safe_runtime_id(model)):
        return None  # an id the wire would refuse is an unresolvable identity
    return provider, model


def _is_fallback_window(compressor: Any, ctx: int) -> bool:
    """True when ``ctx`` is the detection-failed hard default rather than a resolved window."""
    from agent import model_metadata as mm

    if ctx != mm.DEFAULT_FALLBACK_CONTEXT or getattr(compressor, "_config_context_length", None):
        return False
    model, base_url = str(getattr(compressor, "model", "") or ""), str(getattr(compressor, "base_url", "") or "")
    # The fallback memo is keyed on the id resolution looked up (prefix and routing variant stripped).
    stripped = mm._strip_provider_prefix(model)
    candidates = {model, stripped, mm._strip_openrouter_routing_variant(
        stripped, base_url=base_url, provider=str(getattr(compressor, "provider", "") or ""))}
    return any((m, base_url) in mm._FALLBACK_WARNED for m in candidates)


def grounded_input_limit(agent: Any, identity: tuple[str, str]) -> Optional[int]:
    """The runtime input window the compressor holds for ``identity``, or None when unknown."""
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        return None
    if (getattr(compressor, "provider", None), getattr(compressor, "model", None)) != identity:
        return None  # the window belongs to another runtime (MoA preset, unsynced switch)
    if getattr(compressor, "_context_probed", False) and not getattr(compressor, "_context_probe_persistable", False):
        return None  # guessed probe tier, not a provider-reported limit
    # Read the resolved value; never trigger a (possibly network) resolution from accounting.
    if "_resolved_context_length" in vars(compressor):
        ctx = compressor._resolved_context_length
    else:
        ctx = getattr(compressor, "context_length", None)
    if type(ctx) is not int or ctx <= 0 or _is_fallback_window(compressor, ctx):
        return None
    window = getattr(compressor, "_effective_input_window", None)
    if callable(window):
        max_tokens = getattr(compressor, "max_tokens", None)
        limit = window(ctx, max_tokens if type(max_tokens) is int else None)
        return limit if type(limit) is int and limit > 0 else ctx
    return ctx


def capture_context_request(
    agent: Any, response: Any, *, context_prompt_tokens: Optional[int] = None, moa_slot: Any = None,
) -> None:
    """Record one received main-loop provider response (no-op outside a turn). ``context_prompt_tokens``
    is the pre-fold prompt size the context actually held; None reads it from ``response.usage``."""
    acc = active_turn_context_usage(agent)
    if acc is None:
        return
    base_url = str(getattr(agent, "base_url", "") or "").lower()
    if base_url.startswith(("acp://", "acp+tcp://")):
        acc.mark_incomplete("agent-as-provider runs its own model calls")
        return
    usage = getattr(response, "usage", None)
    if context_prompt_tokens is None and usage:
        from agent.usage_pricing import normalize_usage
        try:
            context_prompt_tokens = normalize_usage(
                usage, provider=getattr(agent, "provider", None), api_mode=getattr(agent, "api_mode", None),
            ).prompt_tokens
        except (AttributeError, TypeError, ValueError):
            context_prompt_tokens = None  # an unreadable usage shape is a missing measurement
    if type(context_prompt_tokens) is not int or context_prompt_tokens <= 0:
        acc.mark_incomplete("response without input measurement")
        return
    identity = _effective_identity(agent, moa_slot)
    if identity is None:
        acc.mark_incomplete("no effective provider/model identity")
        return
    acc.add(identity[0], identity[1], context_prompt_tokens, grounded_input_limit(agent, identity))


def note_request_features(agent: Any, api_kwargs: Any) -> bool:
    """Whether THIS request's response is individually measurable. Provider-executed tools (hosted
    web search, OpenRouter plugins) run extra model passes inside one response whose usage may be
    their sum: that response gets no per-request entry (the turn is marked incomplete; billing still
    folds it). Per response, never sticky: later ordinary requests in the turn are still recorded."""
    if active_turn_context_usage(agent) is None or not isinstance(api_kwargs, dict):
        return True
    from agent.side_question_request import has_server_side_tools
    try:
        server_side = has_server_side_tools(api_kwargs)
    except (AttributeError, TypeError):
        server_side = True  # an unreadable request shape cannot be vouched for
    if server_side:
        mark_turn_context_incomplete(agent, "provider-executed tools aggregate internal calls")
        return False
    return True


def capture_summary_context(agent: Any, response: Any) -> None:
    """The iteration-limit summary is a main-runtime request that bypasses ``record_response_usage``.
    A router timeout shim is a reply with no measurement; a MoA summary may fan out to advisors."""
    if active_turn_context_usage(agent) is None:
        return
    from agent.transports.chat_completions import is_router_timeout_shim
    if is_router_timeout_shim(response):
        mark_turn_context_incomplete(agent, "iteration summary router shim not measured")
        return
    moa_slot = None
    if str(getattr(agent, "provider", "") or "").strip().lower() == "moa":
        moa_slot = getattr(getattr(agent, "client", None), "last_aggregator_slot", None)
        mark_turn_context_incomplete(agent, "MoA summary may fan out to advisors")
    capture_context_request(agent, response, moa_slot=moa_slot)
