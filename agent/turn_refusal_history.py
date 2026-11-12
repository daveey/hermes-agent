"""[daveey] Safety refusals the earlier history caused, not the new message.

A refusal on the turn's first model call, before any tool round, judged a request whose only new part is
the user's message. When that message is harmless the trigger is the history, and dropping the refused
turn (the normal recovery) resends the trigger, so every later message is refused too (chat
20261102_175427_594451: three questions refused in a row behind "just ask something else"). Recovery:
compact the earlier history once, retry the same message once, and if the summary or the retry is
refused, say the history is the cause and point at /new.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

HISTORY_REFUSAL_COPY = (
    "{label}'s safety filter refused this request, and summarizing this chat's earlier history didn't get "
    "past it. When a harmless message is refused like this, the earlier history is the trigger and new "
    "messages here will be refused too: start a new chat with /new (in a Bot Chat, Archive it from its tab "
    "menu). If this message itself was sensitive, rewording it may help."
    "\n\nProvider said: {summary}"
)
_COMPACTION_TRIGGER = "content_policy_history"


@dataclass
class HistoryRefusalStep:
    """``outcome``: ``"retry"`` (``messages``/``conversation_history`` hold the compacted request to
    resend), ``"refused"`` (the compacted retry was refused too) or ``"summary_refused"`` (the summary
    over the earlier history did not commit). The last two close the turn with ``HISTORY_REFUSAL_COPY``."""

    outcome: str
    messages: Any = None
    conversation_history: Any = None
    system_prompt: Any = None  # the prompt _compress_context returned (the retry sends it)


def is_history_triggered(messages: Any, current_turn_user_idx: Any) -> bool:
    """The refused request held earlier history plus only this turn's user row (no model round yet)."""
    if not (isinstance(messages, list) and isinstance(current_turn_user_idx, int)
            and 0 < current_turn_user_idx < len(messages)):
        return False
    if messages[current_turn_user_idx + 1:]:
        return False
    return any(isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
               for m in messages[:current_turn_user_idx])


@contextmanager
def _whole_history_window(compressor: Any) -> Iterator[None]:
    """Summarize as much of the head as the compressor allows: the trigger may sit in the first exchange
    or the recent tail that normal compaction keeps verbatim. The compressor still keeps its required
    anchors (latest user turn and reply, the lean tail floor) and the lean summary quotes earlier user
    messages verbatim, so this only removes a trigger in older assistant/tool content. A trigger it keeps
    makes the retry refused (or the summary refused as would-grow); the turn then closes with the copy."""
    saved = {}
    for name, value in (("protect_first_n", 0), ("protect_last_n", 0), ("tail_token_budget", 1)):
        if compressor is not None and hasattr(compressor, name):
            saved[name] = getattr(compressor, name)
            setattr(compressor, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(compressor, name, value)


def _log(agent: Any, outcome: str) -> None:
    logger.warning("%shistory-triggered refusal: compacted+retried outcome=%s",
                   getattr(agent, "log_prefix", ""), outcome)


def _with_current_once(compressed: list, current: Any) -> list:
    """End the retry request with the current message exactly once: drop trailing user rows that only
    repeat its text (a head that already carries it, possibly folded onto itself as ``"U\\n\\nU"``), then
    append it. Appending to such a head sent the message three times."""
    text = current.get("content") if isinstance(current, dict) else None
    while (isinstance(text, str) and text.strip() and compressed and isinstance(compressed[-1], dict)
           and compressed[-1].get("role") == "user" and isinstance(compressed[-1].get("content"), str)
           and text in compressed[-1]["content"] and not compressed[-1]["content"].replace(text, "").strip()):
        compressed.pop()
    last = compressed[-1] if compressed else None
    if not (isinstance(last, dict) and last.get("role") == "user" and last.get("content") == text):
        compressed.append(current)
    return compressed


def history_refusal_step(
    agent: Any, *, messages: Any, current_turn_user_idx: Any, turn_id: Any, system_message: Any,
    effective_task_id: Any,
) -> Optional[HistoryRefusalStep]:
    """``None`` when the refusal is not history-triggered (keep the drop-and-notice path).

    ``system_message`` is the turn loop's caller prompt (``None`` on a normal turn), passed through to
    ``_compress_context`` unchanged like the overflow/recovery callers: the full built prompt there is
    appended to the rebuilt one as a context part and doubles it."""
    if not is_history_triggered(messages, current_turn_user_idx):
        # Checked first: a refusal after this turn's retry ran a tool round keeps the tool-round copy.
        return None
    turn_key = turn_id or getattr(agent, "_current_turn_id", None)
    if not turn_key:
        # No turn identity: the one-retry cap could not tell a refused retry from a new turn and
        # would compact + retry forever. Keep the drop-and-notice path.
        return None
    if getattr(agent, "_history_refusal_retry_turn", None) == turn_key:
        _log(agent, "refused")
        return HistoryRefusalStep("refused")
    head = list(messages[:current_turn_user_idx])
    current = messages[current_turn_user_idx]
    try:
        with _whole_history_window(getattr(agent, "context_compressor", None)):
            compressed, prompt = agent._compress_context(
                head, system_message, task_id=effective_task_id or "default", force=True,
                trigger=_COMPACTION_TRIGGER, verbatim_tail=[current],
            )
    except Exception:
        # Compaction could not run at all: no evidence either way, keep the drop-and-notice path.
        logger.exception("%shistory-triggered refusal: compaction failed", getattr(agent, "log_prefix", ""))
        return None
    lock_skipped = getattr(agent, "_compression_skipped_due_to_lock", None)
    if lock_skipped is True or isinstance(lock_skipped, str):
        _log(agent, "compaction_skipped")
        return None
    if not isinstance(compressed, list) or compressed is head or compressed in (head, [*head, current]):
        # The summary over the earlier history alone did not commit (the summarizer refused or aborted).
        _log(agent, "summary_refused")
        return HistoryRefusalStep("summary_refused")
    compressed = _with_current_once(list(compressed), current)
    idx = len(compressed) - 1
    agent._history_refusal_retry_turn = turn_key  # one compaction + retry per turn
    # "retried": the compacted request is resent; a refusal of it logs outcome=refused next.
    _log(agent, "retried")
    return HistoryRefusalStep("retry", compressed, compressed[:idx], prompt)
