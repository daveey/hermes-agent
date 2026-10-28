"""``FINAL SEND for @session:<ref>: <text>`` lines in a Kanban completion summary.

A worker is refused a ``cross_session`` send into a human chat. The refusal subscribes that chat to the
worker's own card; the worker then writes the message it wanted to send as a ``FINAL SEND`` line in its
``kanban_complete`` summary. ``kanban_db.complete_task`` records the parsed blocks on the ``completed``
event (``payload["final_sends"]``, keyed by session id) and the notifier shows a subscribed chat the block
addressed to it in place of the generic "✔ Kanban <id> done — <summary>" line.

Grammar: a line starting ``FINAL SEND for @session:<profile>[@<gateway>]/<session_id>:`` (optional ``**``/
backtick/``-`` decoration). The text is the rest of that line plus every following line up to the next
``FINAL SEND`` line. A chat only sees a block addressed to its own session id.
"""

from __future__ import annotations

import re
from typing import Optional

MAX_CHARS = 4000

_HEAD = re.compile(
    r"^[\s>*`_-]*FINAL[ _]SEND\s+for\s+`?@session:(?:[A-Za-z0-9_.-]+(?:@[A-Za-z0-9_.-]+)?/)?"
    r"(?P<sid>[A-Za-z0-9_.-]+)`?\s*\**\s*:\s*\**(?P<rest>.*)$",
    re.IGNORECASE,
)


def public_summary(summary: Optional[str]) -> str:
    """Only prose before the first addressed block is a broadcast summary."""
    lines = []
    for line in (summary or "").splitlines():
        if _HEAD.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def parse_final_sends(summary: Optional[str]) -> dict[str, str]:
    """``{session_id: text}`` for every ``FINAL SEND`` block in *summary* (first block per session wins)."""
    if not summary:
        return {}
    blocks: list[tuple[str, list[str]]] = []
    for line in str(summary).splitlines():
        m = _HEAD.match(line)
        if m:
            blocks.append((m.group("sid"), [m.group("rest").strip()]))
        elif blocks:
            blocks[-1][1].append(line.rstrip())
    out: dict[str, str] = {}
    for sid, lines in blocks:
        text = "\n".join(lines).strip()
        if text and sid not in out:
            out[sid] = text[:MAX_CHARS]
    return out


def final_send_for_chat(payload: Optional[dict], chat_id: str) -> Optional[str]:
    """The ``FINAL SEND`` text a ``completed`` event addresses to subscription *chat_id*, or None.
    ``tui`` rows hold the bare session id, ``hub`` rows ``<gw>/<profile>/<sid>``."""
    sends = (payload or {}).get("final_sends")
    if not isinstance(sends, dict) or not chat_id:
        return None
    text = sends.get(str(chat_id).rsplit("/", 1)[-1])
    return text if isinstance(text, str) and text else None
