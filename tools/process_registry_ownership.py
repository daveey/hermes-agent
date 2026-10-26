"""Ephemeral local-process evidence. Never persisted, restored, or inferred from argv.

Only native canonical turn capture can bind a spawn. Readback does not poll,
reap, adopt, consume queues, load retained results, or write checkpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession
from pathlib import Path
import re
import subprocess


@dataclass(frozen=True)
class SpawnScope:
    home: str
    session_id: str
    message_uid: str
    content: str
    turn_id: str


@dataclass(frozen=True)
class ProcessOwnershipSnapshot:
    process_id: str
    generation: str
    home: str
    session_id: str
    message_uid: str
    content: str
    session_key: str


def capture_canonical_scope(agent, canonical) -> None:
    """Called before plugin context injection, never from tool arguments."""
    from hermes_constants import get_hermes_home
    agent._process_spawn_scope = None
    relay = getattr(agent, "_current_turn_id", None)
    if (not canonical or not relay or not agent.session_id
            or not isinstance(canonical.get("content"), str)
            or not 0 < len(canonical["content"]) <= 32000):
        return
    agent._process_spawn_scope = SpawnScope(str(Path(get_hermes_home()).resolve()),
        agent.session_id, canonical["message_uid"], canonical["content"], relay)


def current_spawn_scope():
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.delegation_context import is_delegated_child_context
    from hermes_constants import get_hermes_home
    agent = get_active_subagent_parent()
    scope = getattr(agent, "_process_spawn_scope", None)
    if (is_delegated_child_context() or not isinstance(scope, SpawnScope)
            or getattr(agent, "_turn_user_intervened", False)
            or scope.turn_id != getattr(agent, "_current_turn_id", None)
            or scope.session_id != agent.session_id
            or scope.home != str(Path(get_hermes_home()).resolve())):
        return None
    return scope


def snapshot(registry, process_id):
    """Exact full handle, timed lock, live local Popen/kernel/reader identity only."""
    if not isinstance(process_id, str) or not re.fullmatch(r"proc_[0-9a-f]{12}", process_id):
        return None
    if not registry._lock.acquire(timeout=0.05):
        return None
    try:
        session = registry._running.get(process_id)
        binding = registry._ownership_bindings.get(process_id)
        if session is None or binding is None:
            return None
        scope, process, owner, route = binding
        if (session.process is not process or not isinstance(process, subprocess.Popen)
                or process.returncode is not None or session.pid != process.pid
                or session.exited or session.detached or session.pid_scope != "host"
                or session.env_ref is not None or session._pty is not None
                or session.wsl_chain or session.systemd_unit or session.persist_on_release
                or not session.notify_on_complete or session.owner_task_id != owner
                or session.parent_session_id != scope.session_id
                or not route or session.session_key != route
                or session._reader_finish_requested.is_set()
                or not session._reader_thread or not session._reader_thread.is_alive()
                or session.host_start_time is None
                or not registry._host_pid_is_ours(session.pid, session.host_start_time)):
            return None
        return ProcessOwnershipSnapshot(process_id, registry.ownership_generation,
            scope.home, scope.session_id, scope.message_uid, scope.content, session.session_key)
    finally:
        registry._lock.release()


class ProcessOwnershipMixin:
    """Native explicit handoff and separate non-mutating proof readback."""

    def ownership_snapshot(self, process_id: str):
        """Read-only local native spawn evidence; unsupported/missing state is None."""
        return snapshot(self, process_id)

    def revoke_route_ownership(self, session_key: str):
        """Native user boundary: forget proof, not the jobs or their delivery state."""
        if not session_key:
            return
        with self._lock:
            for pid, binding in tuple(self._ownership_bindings.items()):
                if binding[3] == session_key:
                    self._ownership_bindings.pop(pid, None)

    def transfer_ownership(self, session_id: str, *, from_owner: str, to_owner: str, to_task_id: str,
                           to_session_key: str, note: str = "") -> Optional[ProcessSession]:
        """Move a RUNNING process from one owner to another under the registry lock. Ownership is the ``owner_task_id``
        field: completion notices are stamped from it at exit time and teardown kills by it, so flipping it here is the
        whole transfer. Returns the session, or None when it is unknown, already exited, or not owned by ``from_owner``
        (the caller must not report a transfer that did not happen)."""
        session = self.get(session_id)
        with self._lock:
            if session is None or session.exited or session.owner_task_id != from_owner:
                return None
            self._ownership_bindings.pop(session.id, None)
            session.owner_task_id = to_owner
            session.task_id = to_task_id
            session.session_key = to_session_key
            session.handoff_note = note
            return session

