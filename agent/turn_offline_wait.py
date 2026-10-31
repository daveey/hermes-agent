"""Wait out a local DNS outage inside the turn's retry loop.

Laptop dark wakes may have no working DNS. Wait on an awake-time budget instead of exhausting
provider retries or rotating healthy credentials. DNS success is only a signal to retry the API,
not proof of reachability: cached DNS, proxies, raw-IP endpoints and connection failures with
working resolution remain on the ordinary bounded retry path.

The budget is per model request (including repeated outages on that request), not per entire turn.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

logger = logging.getLogger("agent.conversation_loop")

# How the user stops the wait on this surface (cron has nobody to tell).
_STOP_HINTS = {"cli": "press Esc to stop", "tui": "press Esc to stop", "desktop": "press Esc to stop",
               "api_server": "cancel the request to stop", "cron": ""}
_DEFAULT_STOP_HINT = "send /stop to cancel"


def offline_wait_budget(agent: Any) -> float:
    """Configured ``agent.offline_wait_seconds`` (0 disables)."""
    from agent.retry_utils import DEFAULT_OFFLINE_WAIT_SECONDS

    value = getattr(agent, "_offline_wait_seconds", DEFAULT_OFFLINE_WAIT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_OFFLINE_WAIT_SECONDS
    return max(float(value), 0.0) if math.isfinite(value) else DEFAULT_OFFLINE_WAIT_SECONDS


def offline_notice(agent: Any, host: str) -> str:
    hint = _STOP_HINTS.get(str(getattr(agent, "platform", "") or "").lower(), _DEFAULT_STOP_HINT)
    text = f"⏳ offline — waiting for network ({host} does not resolve); the turn resumes when it is back"
    return f"{text}; {hint}" if hint else text


def wait_out_offline(
    agent: Any, api_error: Any, _retry: Any, *, messages: Any, conversation_history: Any,
    api_call_count: int,
) -> Optional[tuple[str, Optional[dict[str, Any]]]]:
    """``("continue", None)`` when the network came back (retry the SAME attempt, uncounted),
    ``("break", None)`` for a steering correction mid-wait, ``("return", result)`` when the user
    stopped the wait, ``None`` when this is not a local network loss or the wait does not apply."""
    from agent.retry_utils import is_local_network_error, provider_host, wait_for_network

    if not is_local_network_error(api_error):
        return None
    remaining = offline_wait_budget(agent) - float(getattr(_retry, "offline_wait_spent_s", 0.0) or 0.0)
    host = provider_host(getattr(agent, "base_url", "") or "")
    if remaining <= 0 or not host:
        return None

    announced = []

    def _announce_once() -> None:
        if announced:
            return
        announced.append(True)
        notice = offline_notice(agent, host)
        agent._emit_diagnostic_status(notice)
        agent._emit_diagnostic_wait(notice)

    def _tick(spent: float) -> None:
        _announce_once()
        # Keep the gateway / cron inactivity watchdogs from reaping a turn that is only waiting.
        agent._touch_activity(f"offline — waiting for network ({spent:.0f}s awake)")

    def _interrupted() -> bool:
        return bool(getattr(agent, "_interrupt_requested", False))

    agent._touch_activity("offline — waiting for network")
    outcome, spent = wait_for_network(host, budget_s=remaining, is_interrupted=_interrupted,
                                     on_tick=_tick, on_wait=_announce_once)
    _retry.offline_wait_spent_s = float(getattr(_retry, "offline_wait_spent_s", 0.0) or 0.0) + spent
    if outcome == "online":
        logger.info("%sNetwork is back after %.0fs offline — retrying the same attempt", agent.log_prefix, spent)
        if announced:
            agent._emit_diagnostic_status("✅ network is back — resuming")
        return "continue", None
    if outcome == "interrupted":
        from agent.turn_recovery import abort_turn_on_interrupt

        if agent.clear_interrupt(preserve_redirect=True):
            _retry.restart_with_redirected_messages = True
            return "break", None
        return "return", abort_turn_on_interrupt(
            agent, messages, conversation_history, api_call_count,
            abort_message="Interrupt detected while waiting for the network, aborting.",
            interrupt_text="Operation interrupted: waiting for the network to come back.",
        )
    if outcome == "exhausted":
        logger.warning("%sStill offline after %.0fs of awake waiting — falling back to normal retries",
                       agent.log_prefix, float(getattr(_retry, "offline_wait_spent_s", 0.0)))
    return None
