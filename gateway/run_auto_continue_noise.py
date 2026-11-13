"""Auto-continue notes the gateway injected into a chat: recognised so they are never replayed as user text.

Moved out of ``gateway/run.py`` (FILE_LINES cap). ``_PAUSE_REPLAY_NOTE_PREFIX`` is the opening of
``tui_gateway.update_pause.PAUSE_REPLAY_NOTE_PREFIX`` (a paused Desktop request forwarded to this gateway's FIFO,
t_01a1d4920ce3cbf4). It stays a literal so the gateway never imports tui_gateway at load;
tests/gateway/test_auto_continue_note_prefixes.py pins it, and the stripper, to real ``pause_note`` output.
"""

from __future__ import annotations

from typing import Any

_AUTO_CONTINUE_NOTE_PREFIX = "[System note: Your previous turn"
_AUTO_CONTINUE_FALLBACK_PREFIX = "[System note: A new message"
_PAUSE_REPLAY_NOTE_PREFIX = "[System note: A turn of yours paused for a"


def _is_auto_continue_noise(content: Any) -> bool:
    """Return True if this user-message content is a gateway-injected auto-continue note (never replay it)."""
    return isinstance(content, str) and content.startswith(
        (_AUTO_CONTINUE_NOTE_PREFIX, _AUTO_CONTINUE_FALLBACK_PREFIX, _PAUSE_REPLAY_NOTE_PREFIX))


def _strip_auto_continue_noise(content: Any) -> Any:
    """Strip leading persisted auto-continue notes from user text; the trailing real question is preserved."""
    if not _is_auto_continue_noise(content):
        return content
    text = str(content)
    while _is_auto_continue_noise(text):
        end = text.find("]")
        if end < 0:
            return ""
        text = text[end + 1 :].lstrip()
    return text
