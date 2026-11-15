"""In-process native chat custody. Never serialized into subprocess environment."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import os
from pathlib import Path
import sqlite3
import time

_current = ContextVar("origin_turn_custody", default=None)


@contextmanager
def scope(lease):
    """Mask parent custody even for a turn with no durable lease."""
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_origin import gateway_names
    value = None
    if lease is not None:
        value = (lease, os.getpid(), Path(get_hermes_home()).resolve(),
                 get_active_profile_name(), gateway_names()[0])
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def origin():
    """Revalidate the exact holder on every capture; a copied context is no lease."""
    from agent.delegation_context import is_delegated_child_process_context
    from hermes_cli.profiles import get_active_profile_name
    from hermes_constants import get_hermes_home
    from hermes_cli.kanban_origin import gateway_names
    from hermes_cli.kanban_session_provenance import _INTERACTIVE_SOURCES
    value = _current.get()
    if value is None or is_delegated_child_process_context():
        return None
    lease, pid, home, profile, gateway = value
    if (pid != os.getpid() or not gateway or profile == "custom"
            or home != Path(get_hermes_home()).resolve()
            or profile != get_active_profile_name() or gateway != gateway_names()[0]):
        return None
    agent, db = lease.agent, lease.db
    if (not lease.is_turn_active() or lease.stop.is_set() or lease.interrupt_message
            or getattr(agent, "_session_db", None) is not db
            or getattr(agent, "_active_session_turn_lease_holder", None) != lease.holder
            or getattr(agent, "_persist_disabled", False)):
        return None
    from agent.origin_managed_custody import _ManagedCustody
    capability = getattr(agent, '_native_origin_managed_custody', None)
    if type(capability) is _ManagedCustody:
        return capability.origin(value)
    sid = getattr(agent, "session_id", None)
    try:
        if Path(db.db_path).resolve() != home / "state.db":
            return None
        if not sid or db._session_turn_lease_key(sid) != db._session_turn_lease_key(lease.session_id):
            return None
        expires = db.session_turn_lease_expires_at(sid, lease.holder)
        if not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time():
            return None
        row = db.get_session(sid)
    except (AttributeError, OSError, sqlite3.Error, ValueError):
        return None
    if not row or row.get("source") not in _INTERACTIVE_SOURCES:
        return None
    # Source/lineage reads may block while the turn is revoked. This is the
    # admission linearization point, not the earlier provenance read.
    try:
        expires = db.session_turn_lease_expires_at(sid, lease.holder)
    except (AttributeError, OSError, sqlite3.Error, ValueError):
        return None
    if (not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time()
            or not lease.is_turn_active() or lease.stop.is_set() or lease.interrupt_message
            or getattr(agent, "_active_session_turn_lease_holder", None) != lease.holder):
        return None
    return {"gateway": gateway, "profile": profile, "session": sid}


class CapturedOrigin(dict):
    """Revocable turn snapshot consumed only in the originating in-process scope."""

    def __init__(self, qualified):
        super().__init__(origin=qualified)
        self._custody = _current.get()

    def validated(self):
        if self._custody is not _current.get():
            return None
        qualified = origin()
        return {"origin": qualified} if qualified and qualified == self.get("origin") else None
