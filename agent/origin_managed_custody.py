"""Private post-adoption interactive capability, intersected with the actual facade lease.

Environment strings, launch source labels and persistence replay receipts grant no authority.
"""
from dataclasses import dataclass
import os
from pathlib import Path


def _credentials(scope):
    return tuple(sorted((key, value) for key, value in scope.items() if key != 'session_id'))


@dataclass(frozen=True, eq=False)
class _ManagedCustody:
    agent: object
    store: object
    credentials: tuple
    session_id: str
    home: Path
    profile: str
    gateway: str
    pid: int

    def origin(self, custody):
        from agent.origin_worker_custody import _unexpired
        from agent.runtime_session_store import WorkerPersistenceError
        from hermes_cli.kanban_session_provenance import _INTERACTIVE_SOURCES
        lease, pid, home, profile, gateway = custody
        agent, store = self.agent, self.store

        def matches(sid):
            return (lease.agent is agent and lease.db is store
                and getattr(agent, '_native_origin_managed_custody', None) is self
                and getattr(agent, '_session_db', None) is store
                and (pid, home, profile, gateway) == (self.pid, self.home, self.profile, self.gateway)
                and self.pid == os.getpid() and _credentials(store.scope) == self.credentials
                and getattr(agent, 'session_id', None) == sid == store.scope['session_id']
                and not getattr(agent, '_persist_disabled', False)
                and lease.is_turn_active() and not lease.stop.is_set() and not lease.interrupt_message
                and getattr(agent, '_active_session_turn_lease_holder', None) == lease.holder)

        def authorized(observation, sid):
            lineage = observation['assignment_lineage']
            return (_unexpired(observation['expires_at']) and observation['source'] in _INTERACTIVE_SOURCES
                and lineage[0] == self.session_id and lineage[-1] == sid
                and lease.session_id in lineage)

        try:
            with store.lock:
                sid = store.scope['session_id']
                if not matches(sid):
                    return None
                first = store.native_origin_observation(sid, lease.holder)
                if not authorized(first, sid):
                    return None
                # Blocking owner reads can race local revocation or another assignment.
                final = store.native_origin_observation(sid, lease.holder)
                if not authorized(final, sid) or not matches(sid):
                    return None
                return {'gateway': self.gateway, 'profile': self.profile, 'session': sid}
        except (OSError, ValueError, AttributeError, TypeError, KeyError, IndexError, WorkerPersistenceError):
            return None


def bind_adopted_agent(agent, store, *, home, scope):
    """Private execute seam, called only after successful owner adoption and construction."""
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_origin import gateway_names
    from hermes_constants import get_hermes_home
    # Minimal construction shims do not participate; never synthesize a store or lease.
    # (A shim lacking the scoped RPC surface gets no capability, so it can never authorize.)
    if (not isinstance(store, RuntimeSessionStore) or not isinstance(getattr(store, 'scope', None), dict)
            or getattr(agent, '_session_db', None) is not store):
        return
    if (dict(store.scope) != scope or Path(home).resolve() != Path(get_hermes_home()).resolve()
            or agent.session_id != scope['session_id']
            or getattr(agent, '_native_origin_managed_custody', None) is not None):
        raise ValueError('invalid_managed_origin_agent')
    agent._native_origin_managed_custody = _ManagedCustody(agent, store, _credentials(scope),
        scope['session_id'], Path(home).resolve(), get_active_profile_name(), gateway_names()[0], os.getpid())
