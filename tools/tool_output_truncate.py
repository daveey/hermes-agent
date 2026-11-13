"""Head/tail truncation for oversized tool output (terminal, execute_code, MCP results).

One algorithm and one notice wording: 40% head (errors surface early) / 60% tail (the most
recent lines matter most) around a single ``... [<LABEL> TRUNCATED - N <unit> omitted out of
T total] ...`` marker, so downstream code that recognises the marker sees one shape. Limits
live in ``tools/tool_output_limits.py``; line-snapped, path-bearing footers (web/browser,
read_file pagination) are different products and stay separate.
"""

from __future__ import annotations

import re

HEAD_RATIO = 0.4

# Matches every notice ``truncation_notice`` renders, whatever the label/unit.
TRUNCATION_NOTICE_RE = re.compile(
    r"\[[A-Z_ ]{1,32} TRUNCATED - [\d,]{1,20} \w{1,16} omitted out of [\d,]{1,20} total\]")


def contains_truncation_notice(text: str) -> bool:
    return bool(text) and TRUNCATION_NOTICE_RE.search(text) is not None


def truncation_notice(omitted: int, total: int, *, label: str = "OUTPUT", unit: str = "chars") -> str:
    return f"\n\n... [{label} TRUNCATED - {omitted:,} {unit} omitted out of {total:,} total] ...\n\n"


def head_tail_split(budget: int) -> tuple[int, int]:
    """``(head, tail)`` character budgets for ``budget`` total."""
    head = int(budget * HEAD_RATIO)
    return head, budget - head


def truncate_head_tail(text: str, max_chars: int, *, label: str = "OUTPUT") -> str:
    """``text`` unchanged when it fits ``max_chars``; otherwise head + notice + tail (the kept
    text is exactly ``max_chars`` long, the notice rides on top)."""
    if len(text) <= max_chars:
        return text
    head, tail = head_tail_split(max_chars)
    omitted = len(text) - head - tail
    return text[:head] + truncation_notice(omitted, len(text), label=label) + text[-tail:]
