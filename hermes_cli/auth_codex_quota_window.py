"""Conservative evidence for lifting a persisted Codex quota window."""
import math


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quota_restored(payload, reset_at=None):
    """A partial short-window response cannot disprove a named long-window wall.

    Match absolute reset epochs with a minute of provider rounding tolerance.
    Unknown/missing evidence keeps the bench; account/model denial always wins.
    """
    limits = [payload.get("rate_limit") or {}] + [
        extra.get("rate_limit") or {} for extra in (payload.get("additional_rate_limits") or [])
        if isinstance(extra, dict)]
    usages = []
    matched = reset_at is None
    for limit in limits:
        if limit.get("allowed") is False or limit.get("limit_reached") is True:
            return False
        for key in ("primary_window", "secondary_window"):
            window = limit.get(key) or {}
            used = window.get("used_percent")
            if _number(used):
                usages.append(used)
                reset = window.get("reset_at")
                if _number(reset_at) and _number(reset) and abs(reset - reset_at) <= 60:
                    matched = True
    return bool(usages) and matched and all(0 <= used < 100 for used in usages)
