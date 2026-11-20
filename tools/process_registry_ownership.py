"""Which local background jobs die with their owner, for ProcessRegistry (process_identity layer 4).

[daveey] Also ephemeral local-process ownership evidence. Never persisted, restored, or inferred from argv:
only native canonical turn capture can bind a spawn. Readback does not poll, reap, adopt, consume queues,
load retained results, or write checkpoints.
"""
from __future__ import annotations

import re
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from hermes_cli.process_identity import own_process_groups, release_process_groups, spawn_owned

if TYPE_CHECKING:
    from tools.process_registry import ProcessSession


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
    """A local job's process group goes to the owner-death supervisor while the job's own process runs,
    so it dies with this process however this process dies (SIGKILL, OOM, crash): the agent-lifecycle
    sweeps only run while we are alive, and a killed CLI/TUI/worker left every job running on init.
    ``ProcessSession._owned_pgid`` is that group, cleared by the one release so a recycled pgid is
    never unregistered on someone else's behalf. The subclass supplies ``_terminate_host_pid``."""

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

    # Set by recover_from_checkpoint: this host re-adopts the jobs a crashed predecessor left
    # running, so its own jobs must outlive its crash the same way (see _owns_job).
    _jobs_outlive_host = False
    _owned_pgid_lock = threading.Lock()

    def _owns_job(self, persist_on_release: bool) -> bool:
        """Not owned, on purpose: ``persist_on_release`` jobs, which must outlive the agent (#41225);
        every job of a gateway, which re-adopts its jobs from the checkpoint after a crash (flagged by
        recover_from_checkpoint, which every gateway boot runs, checkpoint or not; a supervised gateway
        also before it gets there). The classic CLI, the TUI, the Desktop, ACP, cron and messaging all
        run their tools in a gateway, so their background jobs are not owned; a process that is not a
        gateway (a standalone serve with no session authority, the slash worker, an embedder of
        AIAgent) owns its jobs, and foreground commands are owned everywhere. Also not owned: every job of a
        managed worker, a per-turn interpreter whose jobs the next turn's worker adopts with their
        output log (worker_output_log); and Windows, where the layer-3 job object covers ownership."""
        from agent.runtime_session_store import is_worker_process
        from tools.process_registry import _IS_WINDOWS, _is_supervised_gateway_process

        return not (_IS_WINDOWS or persist_on_release or self._jobs_outlive_host
                    or is_worker_process() or _is_supervised_gateway_process())

    def _spawn_job(self, session: Any, argv: list[str], persist_on_release: bool, **popen_kwargs) -> subprocess.Popen:
        """Start a pipe job in its own session, owned unless ``_owns_job`` says it must outlive us."""
        if self._owns_job(persist_on_release):
            proc = spawn_owned(argv, purpose="terminal-background", **popen_kwargs)
            session._owned_pgid = proc.pid
            return proc
        # noqa: subprocess-stdin — a forwarder: spawn_local passes stdin=DEVNULL in popen_kwargs
        from hermes_cli.worker_drain_fence import popen_worker
        return popen_worker(subprocess.Popen, argv, start_new_session=True, **popen_kwargs)  # detached-spawn: ok — see _owns_job

    def _own_pty_job(self, session: Any, pty_proc: Any) -> None:
        """The PTY child leads its own session and group (pty.fork). Its hangup reaches only jobs that
        keep the default SIGHUP action; `nohup`/trap-HUP servers outlived a killed owner."""
        if self._owns_job(session.persist_on_release):
            own_process_groups([pty_proc.pid])
            session._owned_pgid = pty_proc.pid

    def _take_handoff_group(self, session: Any, proc: subprocess.Popen) -> None:
        """A foreground command that yields to background hands its supervised group over with the
        pipe (LocalEnvironment._run_bash): the job keeps it, unless jobs here must outlive us."""
        owned_pgid, proc._hermes_owned_pgid = getattr(proc, "_hermes_owned_pgid", None), None
        if owned_pgid is not None and self._owns_job(False):
            session._owned_pgid = owned_pgid
        elif owned_pgid is not None:
            release_process_groups([owned_pgid])

    def _return_handoff_group(self, session: Any, proc: subprocess.Popen, owned_pgid: Any) -> None:
        """The handoff failed (the reader could not start): the foreground wait resumes and must own
        the group again, covered by the supervisor, so its own completion releases it."""
        with self._owned_pgid_lock:
            session._owned_pgid = None
        if owned_pgid is not None:
            own_process_groups([owned_pgid])  # re-covers a group the gateway path released
            proc._hermes_owned_pgid = owned_pgid

    def _release_job_group(self, session: Any) -> None:
        """The job's own process exited or was killed: stop covering its group. What it left behind on
        purpose (`server &`) is no longer this job's, exactly as before."""
        with self._owned_pgid_lock:
            owned_pgid, session._owned_pgid = session._owned_pgid, None
        if owned_pgid is not None:
            release_process_groups([owned_pgid])

    def _reap_untracked(self, session: Any, proc: subprocess.Popen) -> None:
        """Post-Popen setup failed: kill the orphaned subprocess (and any setsid
        descendants) so nothing leaks untracked."""
        from tools.process_registry import _stop_systemd_unit

        with suppress(Exception):
            if session.systemd_unit:
                # Scope teardown is the authoritative cleanup for the worker cgroup;
                # the systemd-run wrapper PID is terminated below as fallback.
                _stop_systemd_unit(session.systemd_unit)
            # _terminate_host_pid revalidates the kernel start time captured at
            # spawn before signalling: pgid == pid would only prove the pid leads a
            # group, and a reaped child can be recycled onto an unrelated leader.
            self._terminate_host_pid(proc.pid, session.host_start_time)
        with suppress(Exception):
            proc.wait(timeout=5)
        self._release_job_group(session)
