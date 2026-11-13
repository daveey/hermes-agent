"""What ``printf FORMAT ARGUMENTS`` may print, for the lifecycle guard's printed-payload walk.

The format is not printed as written (``printf '%s %s' bash /x/a.sh`` prints ``bash /x/a.sh``),
so walking the raw argument line reads ``%s`` as the executed command and hides the script that
bash runs. These are extra walk variants only: each can add text to scan, never remove any.
Not modelled (pre-existing, follow-up t_01a1d58e0438e52e): ``*`` width, ``.N`` precision and
``%b`` escapes.
"""

from __future__ import annotations

import re
from typing import Iterator

# One printf conversion (flags, width, precision, conversion letter) or a literal `%%`.
_PRINTF_CONVERSION_RE = re.compile(r"%(%|[-+ #0]*[0-9*]*(?:\.[0-9*]*)?[a-zA-Z])")
_PRINTF_MAX_ROUNDS = 64


def _printf_output(format_text: str, arguments: list[str]) -> str:
    """Roughly what ``printf FORMAT ARGUMENTS`` prints: each conversion takes the next argument
    as text, and the format repeats while arguments remain (as bash's printf does). Only a walk
    variant, so imprecision (``%d`` of a word) can only add a line to scan."""
    remaining = list(arguments)
    rounds: list[str] = []

    def convert(match: re.Match[str]) -> str:
        if match[1] == "%":
            return "%"
        return remaining.pop(0) if remaining else ""

    while len(rounds) < _PRINTF_MAX_ROUNDS:
        before = len(remaining)
        rounds.append(_PRINTF_CONVERSION_RE.sub(convert, format_text))
        if not remaining or len(remaining) == before:
            break
    return "".join(rounds)


def _printf_format_lines(arguments: list[str]) -> Iterator[str]:
    """For printf *arguments* starting at the format: the arguments without the format (covers a
    format the walk cannot read, e.g. ``"$f"``), and the format applied to them."""
    if not arguments:
        return
    yield " ".join(arguments[1:])
    yield _printf_output(arguments[0], arguments[1:])
