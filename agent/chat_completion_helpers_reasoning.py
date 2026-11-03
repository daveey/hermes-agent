"""Reasoning text of a chat-completions assistant message (:mod:`agent.chat_completion_helpers`)."""
import contextlib
import logging
import re
from typing import Optional

from agent.message_content import flatten_message_text
from agent.message_sanitization import _sanitize_surrogates


def assistant_reasoning_text(agent, assistant_message) -> Optional[str]:
    """Structured reasoning, else inline ``<think>`` blocks embedded in content."""
    reasoning_text = agent._extract_reasoning(assistant_message)
    if not reasoning_text:
        content = flatten_message_text(getattr(assistant_message, "content", None))
        think_blocks = re.findall(r'<think>(.*?)</think>', content, flags=re.DOTALL)
        if think_blocks:
            reasoning_text = "\n\n".join(b.strip() for b in think_blocks if b.strip()) or None
    if reasoning_text and agent.verbose_logging:
        logging.debug(f"Captured reasoning ({len(reasoning_text)} chars): {reasoning_text}")
    # When streaming is active the reasoning was already displayed during the
    # stream (structured deltas or <think> tag extraction); fire only for
    # non-streaming modes (gateway, batch, quiet). Anything not shown during
    # streaming is caught by the CLI post-response fallback.
    if reasoning_text and agent.reasoning_callback and not agent.stream_delta_callback and not agent._stream_callback:
        with contextlib.suppress(Exception):
            agent.reasoning_callback(reasoning_text)
    return _sanitize_surrogates(reasoning_text) if reasoning_text else reasoning_text
