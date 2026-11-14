"""Stable rejection classification for structured Kanban tool errors."""
import json


def _tag_rejection(payload: str, rejection: str) -> str:
    """Add ``rejection`` to a tool_error JSON payload that lacks one."""
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return payload
    if not isinstance(data, dict) or "error" not in data or data.get("rejection"):
        return payload
    data["rejection"] = rejection
    return json.dumps(data, ensure_ascii=False)
