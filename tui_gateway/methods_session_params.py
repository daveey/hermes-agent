"""Pure parameter coercion shared by session handlers."""


def _str_param(params: dict, key: str, default: str = "") -> str:
    """``str(params[key]).strip()`` with ``default`` for missing / falsy values."""
    return str(params.get(key) or "").strip() or default


def _int_param(params: dict, key: str, default: int) -> int:
    """``int(params[key])`` with ``default`` for missing / unparsable values."""
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default
