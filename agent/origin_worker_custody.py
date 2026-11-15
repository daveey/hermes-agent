"""Private adopted-worker authority, intersected with each actual facade turn.

Nothing here is serialized. Board ancestry and environment pins are not authority.
"""
from contextlib import closing
from dataclasses import dataclass, field, replace
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time


def _unexpired(value):
    return type(value) in (int, float) and math.isfinite(value) and value > time.time()


class _Handoff:
    def __init__(self):
        self.lock = threading.Lock()
        self.used = False

    def consume(self):
        with self.lock:
            if self.used:
                raise ValueError('invalid_worker_origin_agent')
            self.used = True


@dataclass(frozen=True, eq=False)
class WorkerCustody:
    """Minted only after bind_worker_context verifies its private adopted frame."""
    task_id: str
    run_id: int
    claim_lock: str
    board: str
    path: Path
    home: Path
    profile: str
    gateway: str
    pid: int
    session_id: str
    execution_scope: tuple
    agent: object = None
    store: object = None
    _handoff: _Handoff = field(default_factory=_Handoff, repr=False)

    def bind_agent(self, agent):
        """One explicit handoff to the constructed agent and its adopted RPC store."""
        from agent.runtime_session_store import RuntimeSessionStore
        store = getattr(agent, '_session_db', None)
        if (self.agent is not None or not isinstance(store, RuntimeSessionStore)
                or tuple(sorted(store.scope.items())) != self.execution_scope
                or getattr(agent, 'session_id', None) != self.session_id):
            raise ValueError('invalid_worker_origin_agent')
        self._handoff.consume()
        agent._native_origin_worker_custody = replace(self, agent=agent, store=store)

    def _claim(self, conn):
        from hermes_cli.kanban_db_sync import _db_key
        from hermes_cli.kanban_origin_native import _primary_scope
        if (_db_key(conn) != str(self.path)
                or _primary_scope(conn) != (self.gateway, self.profile, self.board)):
            return False
        row = conn.execute('SELECT t.status,t.current_run_id,t.claim_lock,t.claim_expires,'
            'r.task_id,r.status,r.claim_lock,r.claim_expires,r.ended_at FROM tasks t '
            'JOIN task_runs r ON r.id=? WHERE t.id=?', (self.run_id, self.task_id)).fetchone()
        if (row is None or tuple(row[:3]) != ('running', self.run_id, self.claim_lock)
                or tuple(row[4:7]) != (self.task_id, 'running', self.claim_lock)
                or row[8] is not None or not _unexpired(row[3]) or not _unexpired(row[7])):
            return False
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind='worker_bound' ORDER BY id DESC LIMIT 1", (self.task_id, self.run_id)).fetchone()
        bound = json.loads(row[0]) if row else {}
        return (bound.get('pid'), bound.get('claim_lock')) == (self.pid, self.claim_lock)

    def validated(self, custody, conn=None):
        from agent.delegation_context import is_delegated_child_process_context
        from agent.runtime_session_store import WorkerPersistenceError
        from hermes_cli.profiles import get_active_profile_name
        from hermes_constants import get_hermes_home
        from hermes_cli.kanban_origin import gateway_names
        if custody is None or is_delegated_child_process_context():
            return None
        lease, pid, home, profile, gateway = custody
        agent, store = self.agent, self.store
        def identity_matches():
            return not (agent is None or lease.agent is not agent or lease.db is not store
                or getattr(agent, '_native_origin_worker_custody', None) is not self
                or (pid, home, profile, gateway) != (self.pid, self.home, self.profile, self.gateway)
                or self.pid != os.getpid() or self.home != Path(get_hermes_home()).resolve()
                or self.profile != get_active_profile_name() or self.profile == 'custom'
                or not self.gateway or self.gateway != gateway_names()[0]
                or getattr(agent, '_session_db', None) is not store
                or getattr(agent, 'session_id', None) != self.session_id
                or lease.session_id != self.session_id
                or tuple(sorted(store.scope.items())) != self.execution_scope
                or getattr(agent, '_persist_disabled', False))
        if not identity_matches():
            return None
        def live_turn():
            expiry = store.session_turn_lease_expires_at(self.session_id, lease.holder)
            # The RPC can block while local cancellation revokes a copied scope.
            return (_unexpired(expiry) and lease.is_turn_active() and not lease.stop.is_set()
                    and not lease.interrupt_message
                    and getattr(agent, '_active_session_turn_lease_holder', None) == lease.holder)
        try:
            if not live_turn():
                return None
            if conn is None:
                from hermes_cli import kanban_lockguard
                with closing(kanban_lockguard.connect(self.path.as_uri() + '?mode=ro', uri=True)) as reader:
                    reader.execute('BEGIN')
                    valid = self._claim(reader)
            else:
                # create_task already owns the write transaction. Do not read a
                # different connection's pre-transaction claim behind that lock.
                valid = self._claim(conn)
            return {'creator': self.task_id} if valid and live_turn() and identity_matches() else None
        except (OSError, sqlite3.Error, ValueError, AttributeError, TypeError, WorkerPersistenceError):
            return None


def from_bound_frame(frame, context):
    """Called by the private bind seam after its exact claim transaction."""
    from hermes_cli.profiles import get_active_profile_name
    from hermes_cli.kanban_origin import gateway_names
    return WorkerCustody(context['task_id'], context['run_id'], context['claim_lock'], context['board'],
                         Path(context['db']).resolve(), Path(frame['home']).resolve(),
                         get_active_profile_name(), gateway_names()[0], os.getpid(),
                         frame['scope']['session_id'], tuple(sorted(frame['scope'].items())))


class CapturedWorkerOrigin:
    """Revocable snapshot, usable only by its exact live facade scope."""
    def __init__(self, capability, custody):
        self._capability, self._custody = capability, custody

    def validated(self, conn=None):
        from agent.origin_turn_custody import _current
        if self._custody is not _current.get():
            return None
        return self._capability.validated(self._custody, conn)


def capture(creator):
    from agent.origin_turn_custody import _current
    custody = _current.get()
    if custody is None:
        return None
    capability = getattr(custody[0].agent, '_native_origin_worker_custody', None)
    if type(capability) is not WorkerCustody or creator != capability.task_id:
        return None
    captured = CapturedWorkerOrigin(capability, custody)
    return captured if captured.validated() else None
