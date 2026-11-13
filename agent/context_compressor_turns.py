"""Turn boundaries for ContextCompressor's in-flight request detection.

A request is in flight only while its turn has not ended. Notice-started turns (Kanban notices,
background-process and delegation reports) have no actionable user row, so the newest human row
can be a request answered several turns ago; restating it after a mid-turn compaction makes the
model redo finished work.
"""

from typing import Any, Callable

# User-row kinds that start a new turn (persisted notices, not same-turn continuations such as
# auto_continue or steer): after a completed reply, one of these means the earlier request is done.
TURN_STARTING_NOTICE_KINDS = frozenset(
    {"kanban_notice", "internal_notification", "process_complete", "async_delegation_complete", "loop_tick"}
)


def turn_ended_after(messages: list[dict[str, Any]], idx: int) -> bool:
    """Whether the turn begun at *idx* already ended: a completed reply, then a turn-starting notice row.

    A completed reply is a text-bearing assistant row with no ``tool_calls`` that is not a summary
    carrier. Same-turn rows (auto_continue, steer, scaffolding, unknown kinds) never end the turn.
    """
    from agent.context_compressor import ContextCompressor, _content_text_for_contains

    replied = False
    for msg in messages[idx + 1:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant" and not msg.get("tool_calls") and not ContextCompressor._is_context_summary_message(msg):
            replied = replied or bool(_content_text_for_contains(msg.get("content")).strip())
        elif replied and role == "user" and msg.get("display_kind") in TURN_STARTING_NOTICE_KINDS:
            return True
    return False


def newest_live_intent(messages: list[dict[str, Any]], intent_of: Callable[[Any], Any]) -> Any:
    """The newest row's intent (``intent_of`` truthy), or None when no row has one or its turn already ended.

    Used by the post-compaction anchor pass: a request answered before a notice-started turn is finished
    work, and every older row's turn ended earlier still, so the scan stops at the newest intent row.
    """
    for idx in range(len(messages) - 1, -1, -1):
        if intent := intent_of(messages[idx]):
            return None if turn_ended_after(messages, idx) else intent
    return None
