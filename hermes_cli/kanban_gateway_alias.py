"""Former names of a Board gateway that was renamed in place. [daveey]

A Board gateway can be renamed without re-enrolling (``mini`` became ``daveey-mini-1``): the key, the cards and
the hub.db file stay, only the name moves. Everything that stored the old name keeps it: a parked
``hermes kanban wait-check task <id> --gateway mini``, a ``wait-comment ... --from-gateway mini``, a comment's
``origin_gateway`` and its relay author ``hub:gw:mini``, an origin ref ``@session:default@mini/<id>``. The Board
keeps the old name as a permanent alias on its side; this module is the local half.

The map is ``kanban.placement.aliases`` (``{former or host-style name: gateway}``, the same map
:func:`hermes_cli.kanban_placement.canonical_pin` reads). It is config, read from disk: nothing here opens a
socket, so a wake probe can call it. Names compare case-insensitively and come back lowercased, as
``canonical_pin`` does.

The placement map is not rename-direction evidence: before cutover it can point from the future name to the
old name. Preserve an explicitly present partition; only an absent name may fall back to its sole known
alias-equivalent partition. No tombstone or authoritative rename fact is inferred from configuration.
"""
from __future__ import annotations

from typing import Iterable, Optional

_MAX_HOPS = 8  # ``mac-mini -> mini -> daveey-mini-1``: chains are short; a longer one is a config loop


def _norm(name) -> str:
    return str(name or "").strip().lower()


def aliases(cfg: Optional[dict] = None) -> dict[str, str]:
    """``{name: target}`` from ``kanban.placement.aliases``, lowercased; ``{}`` when unset or unreadable."""
    try:
        if cfg is None:
            from hermes_cli import kanban_placement
            cfg = kanban_placement.config()
        raw = cfg.get("aliases") if isinstance(cfg, dict) else None
        if not isinstance(raw, dict):
            return {}
        out = {_norm(k): _norm(v) for k, v in raw.items()}
        return {k: v for k, v in out.items() if k and v and k != v}
    except (ImportError, AttributeError, TypeError, ValueError):  # no readable map = no aliases, never a failed probe
        return {}


def resolve(name, cfg: Optional[dict] = None, *, table: Optional[dict[str, str]] = None) -> str:
    """The end of ``name``'s alias chain (``name`` itself when it has none, or when the chain loops)."""
    table = aliases(cfg) if table is None else table
    start = seen = _norm(name)
    for _ in range(_MAX_HOPS):
        nxt = table.get(seen)
        if not nxt:
            return seen
        seen = nxt
    return start


def current(name, known: Iterable[str], cfg: Optional[dict] = None) -> str:
    """Preserve a known explicit name; otherwise use its unique known equivalent, in either map direction.

    ``known`` is the set of gateways this host has records of (``kanban_origin.gateway_names()[1]``).
    Multiple surviving equivalents are ambiguous, so an absent name is not redirected to any of them.
    """
    raw = str(name or "").strip()
    present = {_norm(gw): str(gw) for gw in known or ()}
    if _norm(raw) in present:
        return present[_norm(raw)]
    candidates = {present[s] for s in spellings(raw, cfg) if s in present}
    return next(iter(candidates)) if len(candidates) == 1 else raw


def spellings(name, cfg: Optional[dict] = None) -> list[str]:
    """Every name of ``name``'s gateway, lowercased, ``name`` first: its alias target and the other aliases of
    that target. A comment written before the rename carries the old name and one written after it the new one;
    a filter on either must match both."""
    table = aliases(cfg)
    first = _norm(name)
    if not first:
        return []
    target = resolve(first, table=table)
    out = [first]
    for other in [target, *sorted(k for k in table if resolve(k, table=table) == target)]:
        if other and other not in out:
            out.append(other)
    return out
