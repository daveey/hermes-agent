"""Factories for the pure-data auxiliary model defaults."""


def auxiliary_defaults(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary block; MoA slots omit reasoning_effort."""
    result = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout, "extra_body": {}}
    if reasoning_effort:
        result["reasoning_effort"] = ""
    result.update(extra)
    return result
