"""Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995). [daveey]

Split out of ``kanban_db_dispatch`` (code-health file cap); re-exported there.
"""

from __future__ import annotations

import logging

from typing import Mapping
from typing import Optional

_log = logging.getLogger("hermes_cli.kanban_db")  # the dispatcher's own log name


def _dispatch_profile_allowlist(normalize_profile_name) -> Optional[frozenset]:
    """Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995).

    On a shared board (one ``kanban.db`` mounted across several Hermes homes),
    every home's ``profile_exists`` returns True for ``default`` — the root
    profile every home has — so a card assigned to ``default`` is claimable by
    every home's dispatcher. A home opts out of foreign claims by declaring
    which assignees it may claim::

        kanban:
          dispatch_profiles: ["sage", "researcher"]   # or "sage,researcher"

    Returns ``None`` only when the key is absent from the user config (upstream
    behavior: any existing profile is claimable). A present value is fail-closed:
    an empty list, ``null`` or a bare ``dispatch_profiles:`` claims nothing, and
    so does a non-mapping ``kanban`` section (a bare ``kanban:`` is absent) or an
    unreadable config (#113620, #147532) — the user layer is read without the
    ``DEFAULT_CONFIG`` merge, whose ``None`` placeholder fakes the key's presence.
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban")
        if kanban is not None and not isinstance(kanban, Mapping):
            raise TypeError(f"the `kanban` section is {type(kanban).__name__}, not a mapping")
    except Exception as exc:
        _log.warning(
            "kanban: kanban.dispatch_profiles cannot be resolved (%s: %s) — this home claims no cards",
            type(exc).__name__, exc,
        )
        return frozenset()
    if "dispatch_profiles" not in (kanban or {}):
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for n in names:
        try:
            allowed.add(normalize_profile_name(n))
        except ValueError:
            continue
    return frozenset(allowed)


def dispatch_profile_allowlist_summary() -> str:
    """What this home may claim, for ``hermes kanban diagnostics`` (#113620): ``any``
    (key absent; also when profiles cannot import, as the gate then stays open too),
    the sorted allowed names, or ``none (fail-closed: ...)``, as `_profile_exists_fn`."""
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        if _dispatch_profile_allowlist(str) is None:
            return "any"
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")
