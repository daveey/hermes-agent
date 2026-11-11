"""Scalar conversions shared by Kanban database writers."""
from __future__ import annotations

import json
from typing import Any, Optional


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
