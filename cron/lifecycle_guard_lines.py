"""How the lifecycle guard's command scan splits a command into logical shell lines.

Moved out of ``cron/lifecycle_guard.py`` to keep that module under its FILE_LINES cap.
"""

from __future__ import annotations


def _split_logical_lines(text: str) -> list[str]:
    """Split on newlines outside quotes (a quoted newline is data, not a separator); honors
    escapes."""
    lines: list[str] = []
    current: list[str] = []
    in_single = in_double = escape = False
    for ch in text:
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "\n" and not in_single and not in_double:
            lines.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        lines.append("".join(current))
    return lines
