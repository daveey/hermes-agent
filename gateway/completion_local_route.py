"""Raw legacy Desktop completion keys, resolved only through the one authority that owns the local receipt."""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

# Any of these makes the event an explicit route (messaging or API), never a bare Desktop session id.
_ROUTE_FIELDS = ("platform", "chat_id", "chat_type", "thread_id", "origin_session_id")


def _owner_route(authority, runner, key: str, delegation_id: str):
    """The canonical local route of *key* when *authority* provably owns it, else None."""
    from gateway.run import _parse_session_key
    from gateway.session_local_recovery import owned_receipt
    try:
        _receipt, _source, route, _entry = owned_receipt(authority, key)
        # The durable claim runs in the ambient ledger: only the owner's own ledger may hold this row, or
        # claim_completion_delivery's missing-row ``True`` would claim (and settle) it in the wrong store.
        with authority.db._read_ctx() as conn:
            row = conn.execute("SELECT 1 FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
    except Exception:  # no receipt, foreign store, no ledger table, refused or unavailable
        logger.debug("Legacy completion route could not be verified", exc_info=True)
        return None
    registry = getattr(runner, "session_authorities", None)
    namespace = registry.profile_name(authority) if registry is not None else None  # profile_id is a HOME path
    parsed = _parse_session_key(route) or {}
    if (row is None or parsed.get("platform") != "local" or parsed.get("chat_id") != key
            or parsed.get("profile") != namespace):
        return None
    return route


def resolve_legacy_local_event(runner, evt: dict) -> dict:
    """*evt* with its canonical local ``session_key`` when it is a legacy raw Desktop event, else *evt*.

    Legacy rows persisted the raw Desktop session id as ``session_key`` (equal to ``parent_session_id``)
    with no platform, chat or origin fields. Runs before scope selection, so it asks EVERY serving
    authority, not the ambient one: exactly one must hold a verified local receipt for that id, own its
    route namespace and hold the delegation row in its ledger. Zero or several matches (API events, other
    profiles, unknown ids, ambiguity) return *evt* unchanged.
    """
    if evt.get("type") != "async_delegation":
        return evt
    key = str(evt.get("session_key") or "")
    delegation_id = str(evt.get("delegation_id") or "")
    if (not key or not delegation_id or key.startswith("agent:")
            or key != str(evt.get("parent_session_id") or "")
            or any(evt.get(field) for field in _ROUTE_FIELDS)):
        return evt
    from gateway.session_authorities import serving_authorities
    routes = [route for authority in serving_authorities(runner)
              if (route := _owner_route(authority, runner, key, delegation_id)) is not None]
    if len(routes) != 1:
        if routes:
            logger.warning("Legacy completion %s is claimed by several local receipts; not routing it", delegation_id)
        return evt
    return dict(evt, session_key=routes[0])
