"""Validate post-loop budget output before it becomes visible or durable."""

from __future__ import annotations


def validate_budget_response(agent, response, messages):
    """No retry budget remains: run callbacks at their exhaustion boundary.

    Return the validated text and whether it replaced the candidate. Native
    replay carriers on a rejected summary must not retain the original content.
    """
    from agent.pre_final_gate import evaluate, max_attempts

    try:
        if getattr(agent, "_interrupt_requested", False):
            replacement = "Response interrupted before validation."
        else:
            decision = evaluate(agent, response, messages, max_attempts())
            replacement = decision.replacement
            if decision.unresolved_fallback is not None:
                replacement = decision.unresolved_fallback.render("turn budget exhausted")
            if replacement is None and decision.append:
                replacement = f"{response}\n\n{decision.append}"
            if getattr(agent, "_interrupt_requested", False):
                replacement = "Response interrupted before validation."
        if replacement is None:
            agent._flush_final_gate_buffer()
            return response, False
        for row in reversed(messages):
            if row.get("role") == "assistant" and row.get("content") == response:
                row.clear()
                row.update(role="assistant", content=replacement)
                agent._db_flush_scan_prefix = None
                break
        agent._response_was_previewed = False
        agent._reused_response_text = None
        return replacement, True
    finally:
        agent._discard_final_gate_buffer()
        agent._final_gate_buffer = None
