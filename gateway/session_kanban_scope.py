"""Place a managed Kanban worker in its run's per-run systemd scope (Linux cgroup v2).

The dispatcher's submitter runs inside ``hermes-worker-kanban-<task>-run-<n>.scope`` (memory-capped),
but the real worker is a Popen child of the gateway and would stay in the uncapped gateway unit.
Right after the spawn, while the child still waits for its bootstrap and the Popen is unreaped (so
the PID cannot be reused), the child's PID is written to the scope's ``cgroup.procs``. Descendants
inherit the cgroup. No scope claimed (non-Linux, no systemd, submitter not in a run scope): unchanged.
A claimed scope that cannot be joined fails closed: the new child is killed and reaped.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

CGROUP_ROOT = Path('/sys/fs/cgroup')
_UNIT = re.compile(r'hermes-worker-kanban-(?P<task>.+)-run-(?P<run>\d+)\.scope')


class ScopePlacementError(RuntimeError):
    pass


def _cgroup_path(pid):
    """The unified-hierarchy cgroup of ``pid`` ('/a/b'), or None if unreadable/not v2."""
    try:
        text = Path(f'/proc/{int(pid)}/cgroup').read_text(encoding='utf-8')
    except (OSError, ValueError):
        return None
    for line in text.splitlines():
        if line.startswith('0::'):
            return line[3:]
    return None


def _own_cgroup():
    """This process's unified-hierarchy cgroup (where a Popen child starts), or None."""
    return _cgroup_path(os.getpid())


def resolve_run_scope(policy):
    """The existing cgroup directory of this attempt's run scope, or None when none is claimed.

    Raises ScopePlacementError when a run scope is claimed but inconsistent (stale claim, wrong scope).
    """
    if not sys.platform.startswith('linux') or not policy or not getattr(policy, 'kanban_json', None):
        return None
    if not (CGROUP_ROOT / 'cgroup.controllers').exists():
        return None
    context = json.loads(policy.kanban_json)
    task_id, run_id, claim, db = (context.get(k) for k in ('task_id', 'run_id', 'claim_lock', 'db'))
    if not (task_id and isinstance(run_id, int) and claim and db):
        return None
    try:
        conn = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise ScopePlacementError(f'kanban_scope_db_unreadable: {exc}') from exc
    try:
        # Popen can exec the submitter before the dispatcher publishes its PID. This
        # runs off-loop before spawn: wait boundedly, revalidating the claim each time.
        deadline = time.monotonic() + 10
        while True:
            task = conn.execute('SELECT status, current_run_id, claim_lock FROM tasks WHERE id=?', (task_id,)).fetchone()
            run = conn.execute('SELECT worker_pid FROM task_runs WHERE id=? AND task_id=? AND status=? AND claim_lock=?',
                               (run_id, task_id, 'running', claim)).fetchone()
            if not task or tuple(task) != ('running', run_id, claim) or not run:
                raise ScopePlacementError('kanban_scope_stale_claim')
            if run[0] or time.monotonic() >= deadline:
                break
            threading.Event().wait(.05)
    except sqlite3.Error as exc:
        raise ScopePlacementError(f'kanban_scope_db_unreadable: {exc}') from exc
    finally:
        conn.close()
    submitter = run[0]
    path = _cgroup_path(submitter) if submitter else None
    if not path:
        raise ScopePlacementError('kanban_scope_submitter_missing')
    match = _UNIT.fullmatch(path.rsplit('/', 1)[-1])
    if match is None:
        return None  # submitter not in a per-run scope (no systemd-run): unchanged
    if (match['task'], int(match['run'])) != (task_id, run_id):
        if path == _own_cgroup():
            # The submitter inherited an enclosing run's scope that this gateway shares (a dispatcher
            # run unsupervised inside another Kanban worker, e.g. a test from a worker shell). The
            # child is born in that same cgroup, so it stays under that scope's limit: unchanged.
            return None
        raise ScopePlacementError('kanban_scope_mismatch')
    target = CGROUP_ROOT / path.lstrip('/')
    if not (target / 'cgroup.procs').is_file():
        raise ScopePlacementError('kanban_scope_missing')
    return target


def migrate(pid, target):
    """Write ``pid`` into ``target``'s cgroup.procs and confirm via /proc; no shell."""
    expected = '/' + str(target.relative_to(CGROUP_ROOT)).strip('/')
    try:
        fd = os.open(target / 'cgroup.procs', os.O_WRONLY)
        try:
            os.write(fd, f'{pid}\n'.encode())
        finally:
            os.close(fd)
    except OSError as exc:
        raise ScopePlacementError(f'kanban_scope_migrate_failed: {exc}') from exc
    if _cgroup_path(pid) != expected:
        raise ScopePlacementError('kanban_scope_unconfirmed')


def _deliver(rendezvous, process):
    from gateway.session_kanban_capsule import MISSING
    from gateway.session_kanban_rendezvous import RendezvousError
    from hermes_state_runtime import RuntimeStoreError
    try:
        rendezvous.deliver(process)
    except RendezvousError as exc:
        raise RuntimeStoreError(MISSING) from exc
    finally:
        rendezvous.close()


def placing_spawn(spawn, policy):
    """``spawn`` wrapped to run synchronously: resolve scope, spawn, migrate, or kill+reap and raise.

    Meant for ``acquire_process``'s worker thread, which owns the returned handle afterwards.
    """
    def run(*args, **kwargs):
        # [daveey] Launch-time recheck (covers admissions queued by an older owner): fail closed. A
        # capsule-required card gets its authenticated, single-use rendezvous here (None: ordinary card,
        # launch unchanged); the child's channel is brokered only after the spawn and scope placement.
        from gateway.session_kanban_capsule import attach_capsule_transport, check_capsule_transport
        from gateway.session_kanban_rendezvous import observing_spawn
        # [daveey] run27: a missing transport refuses first (non-consuming probe), then the run scope is
        # resolved, and only then is the single-use rendezvous consumed, so a scope failure (stale claim,
        # scope not ready) refuses without burning the run.
        check_capsule_transport(policy)
        target = resolve_run_scope(policy)  # validated before anything is spawned or consumed
        from hermes_cli.worker_compute_capacity import managed_target, lower_priority
        compute = managed_target(policy, target)
        from gateway.session_kanban_source_env import source_worker_env
        env = source_worker_env(policy, kwargs.get('env'))
        if env is not kwargs.get('env'):
            kwargs['env'] = env
        rendezvous = attach_capsule_transport(policy)
        try:
            process = observing_spawn(spawn, rendezvous)(*args, **kwargs)
        except BaseException:
            if rendezvous is not None:
                rendezvous.close()
            raise
        if target is None and rendezvous is None:
            return process
        try:
            if target is not None:
                migrate(process.pid, target)
            if compute:
                lower_priority(process.pid)
            if rendezvous is not None:
                _deliver(rendezvous, process)
        except BaseException:
            from gateway.session_managed_worker import ManagedWorker
            if rendezvous is not None:
                rendezvous.close()
            try:
                ManagedWorker(process).close()
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            raise
        return process
    return run
