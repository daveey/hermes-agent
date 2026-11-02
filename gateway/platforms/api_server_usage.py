"""API wire ``usage`` blocks built from a finished turn's usage dict (``_finish_turn_result``, or the
canonical admission's committed usage), plus the optional per-provider-response ``context_usage``."""
from __future__ import annotations

from typing import Any, Optional

_USAGE_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")
_CONTEXT_REQUEST_KEYS = ("provider", "model", "input_tokens", "input_limit")


def api_context_usage(value: Any) -> Optional[dict[str, Any]]:
    """The turn's per-provider-response context accounting (``agent/context_usage.py``,
    ``result["context_usage"]``) as the API wire carries it, or None when absent or malformed: an
    unavailable measurement is omitted, never reconstructed. Returns a fresh plain-JSON copy."""
    from agent.context_usage import CONTEXT_USAGE_VERSION, MAX_CONTEXT_REQUESTS, is_wire_safe_runtime_id
    if (not isinstance(value, dict) or set(value) != {"version", "complete", "requests"}
            or type(value["version"]) is not int or value["version"] != CONTEXT_USAGE_VERSION
            or type(value["complete"]) is not bool or not isinstance(value["requests"], list)
            or len(value["requests"]) > MAX_CONTEXT_REQUESTS):
        return None
    requests = []
    for entry in value["requests"]:
        if not isinstance(entry, dict) or set(entry) != set(_CONTEXT_REQUEST_KEYS):
            return None
        provider, model, tokens, limit = (entry[key] for key in _CONTEXT_REQUEST_KEYS)
        # Identity ids follow the served_runtime bound: <=80 chars, no CR/LF/NUL.
        if (not is_wire_safe_runtime_id(provider) or not is_wire_safe_runtime_id(model)
                or type(tokens) is not int or tokens < 0
                or not (limit is None or (type(limit) is int and limit > 0))):
            return None
        requests.append(dict(zip(_CONTEXT_REQUEST_KEYS, (provider, model, tokens, limit))))
    return {"version": value["version"], "complete": value["complete"], "requests": requests}


def with_context_usage(target: dict[str, Any], source: Any) -> dict[str, Any]:
    """``target`` plus ``source["context_usage"]`` when it is a valid report. It rides beside (never
    inside) the billing sums, which stay exactly as they were."""
    context_usage = api_context_usage(source.get("context_usage")) if isinstance(source, dict) else None
    if context_usage is not None:
        target["context_usage"] = context_usage
    return target


def _chat_usage_payload(usage: dict[str, Any]) -> dict[str, Any]:
    """OpenAI Chat Completions ``usage`` block (prompt/completion/total) from the agent's usage, plus
    the optional ``context_usage``; streaming and non-streaming both serialize through here."""
    values = (usage.get(key, 0) for key in _USAGE_TOKEN_KEYS)
    return with_context_usage(dict(zip(("prompt_tokens", "completion_tokens", "total_tokens"), values)), usage)


def _responses_usage_payload(usage: dict[str, Any]) -> dict[str, int]:
    """OpenAI Responses ``usage`` block from the agent's usage dict."""
    return {key: usage.get(key, 0) for key in _USAGE_TOKEN_KEYS}
