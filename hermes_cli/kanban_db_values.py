"""Scalar conversions shared by Kanban database writers."""
from __future__ import annotations

import json
import re
from typing import Any, Optional


def _lossy_text(value: Any) -> Any:
    """``bytes`` -> ``str`` with U+FFFD for undecodable sequences; anything else passes through.

    Installed as every board connection's ``text_factory`` (a TEXT cell holding
    invalid UTF-8 otherwise aborts the whole ``fetchall`` with "Could not decode
    to UTF-8") and applied to BLOB-typed cells in the ``from_row`` constructors
    (task, comment, event, run), so one
    corrupt row degrades to replacement characters instead of taking the board
    listing down."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _first_line(text: Optional[str], limit: int) -> str:
    """First non-blank-stripped line of ``text`` capped at ``limit`` chars; "" when empty."""
    lines = (text or "").strip().splitlines()
    return lines[0][:limit] if lines else ""


def _opt_int(value: Any) -> Optional[int]:
    """``int(value)`` or ``None`` when ``value`` is ``None`` (NULL column passthrough)."""
    return int(value) if value is not None else None


def _json_or_null(obj: Any) -> Optional[str]:
    """JSON text for a payload/metadata column; falsy -> NULL."""
    return json.dumps(obj, ensure_ascii=False) if obj else None


def error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group.

    Normalize BEFORE keeping 80 chars: truncating first let the PID's digit count move the cut, so
    ``pid 99999`` and ``pid 100000`` dying of the same error kept different tails of the worker's
    output and never reached the systemic same-fingerprint count. [daveey]
    """
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text)
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp[:80].lower().strip()
