"""Independent board-only failed-result observer. [daveey]

Pre-armed before the result commit, so a dying writer cannot strand a committed
result in a commit-to-launch gap. Never reads the owner's session store.
"""
from __future__ import annotations

import contextlib
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

READY = b'failed-result-observer-ready\n'
POLL_SECONDS = 0.5
START_SECONDS = 5.0
# Bound retries for the short relocation fence, never the dispatcher tick lock.
OPEN_SECONDS = 2.0
# EX_CONFIG: deterministic startup failure; the service never restarts on it.
STARTUP_FAILURE_EXIT = 78
CONTEXT_KEYS = ('db', 'task_id', 'run_id', 'claim_lock')


def arm(context):
    """Wait for a detached observer to open this board before recording failure.

    Scope-unavailable/startup errors are reported by the caller, not hidden as
    a successful arm. No model/provider credentials are needed by the child.
    """
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import restart_safe_gateway_child_argv, systemd_user_bus_env
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    from hermes_cli._startup_fast import project_root_str

    context = {key: context[key] for key in ('db', 'task_id', 'run_id', 'claim_lock', 'board') if key in context}
    context.update(writer_pid=os.getpid(), writer_birth=_process_fingerprint(os.getpid()))
    if sys.platform == 'linux':
        from hermes_cli.kanban_failed_result_service import arm_service
        return arm_service(context, startup_seconds=START_SECONDS)
    from hermes_cli.kanban_failed_result_service import observer_argv_env
    command, env = observer_argv_env(build_subprocess_env(extra=systemd_user_bus_env()), context)
    launch = restart_safe_gateway_child_argv(
        command, unit_suffix=f'failed-result-{os.getpid()}-{time.monotonic_ns()}',
        require_restart_safe_scope=True, outlives_parent=True)
    if launch.mode == 'degraded':
        raise RuntimeError('independent failed-result observer scope unavailable')
    proc = subprocess.Popen(
        launch.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        # detached-spawn: ok — independent reconciler must outlive the failed writer to close its run
        start_new_session=True, close_fds=True,
        env=env,
        cwd=project_root_str(),
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + START_SECONDS
    received = b''
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
            if readable:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                received += chunk
                if READY in received:
                    return proc.pid
            if proc.poll() is not None:
                break
    finally:
        proc.stdout.close()
    raise RuntimeError('independent failed-result observer did not acknowledge startup')


@contextlib.contextmanager
def connect_existing(path):
    """Bounded native access without schema/init locks or board creation.

    The result writer has already initialized this board. Preserve native inode,
    generation and raw-file guards, but do not repeat migrations in the observer.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_dispatch_owner import connect_with_identity
    from hermes_state_guard import ensure_kanban_test_isolation

    path = Path(path)
    ensure_kanban_test_isolation(path)
    deadline = time.monotonic() + OPEN_SECONDS
    while True:
        try:
            conn = connect_with_identity(path, existing_only=True, generation_only=True,
                                         isolation_level=None, timeout=.25)
            break
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.05)
    with contextlib.closing(conn):
        conn.row_factory = sqlite3.Row
        conn.text_factory = kb._lossy_text
        conn.execute('PRAGMA busy_timeout=250')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA secure_delete=ON')
        conn.execute('PRAGMA cell_size_check=ON')
        # Missing/old schema fails visibly instead of being migrated here.
        conn.execute('SELECT current_run_id, claim_lock FROM tasks LIMIT 0')
        conn.execute('SELECT ended_at FROM task_runs LIMIT 0')
        # This process never ran the board's init, which is what registers a Board-attached file as synced; without
        # it board_mode() reads "off" and a primary board's failure is closed in the local mirror instead of
        # released to the Board (worker-a1, t_01a1d65b80d2e9f4). Writes nothing.
        from hermes_cli import kanban_db_sync as sync
        sync.register_without_install(conn, sync._db_key(conn) or str(path))
        yield conn


def _generation(conn):
    """The board path iff this handle's inode is still the one at that path."""
    from hermes_cli.kanban_dispatch_owner import connection_db_path
    try:
        return connection_db_path(conn)
    except (OSError, ValueError):
        # Archived, deleted, replaced or never pinned: not ours to touch.
        return None


def _finalize(conn, context, path):
    """Close a proven failed run only inside its own board generation.

    ``path`` comes from the caller's lock-free generation check. Returns False
    when the generation changed under the lock (terminate). The independent
    generation lock is a single non-blocking attempt. Native archive/delete take
    the same fence after dispatch, excluding relocation through closure without
    depending on dispatcher progress.
    """
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import _dispatch_tick_lock
    from hermes_cli.kanban_failed_result import failed_result_proof

    if not failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']):
        return True
    key = path.with_name(path.name + '.generation')
    with _dispatch_tick_lock(key, create_parent=False) as held:
        if not held:
            return True
        # The path may have changed between the lock-free check and the lock.
        if _generation(conn) is None:
            return False
        # The sweep revalidates the exact run/claim under its write transaction
        # (a dispatcher may have ended it). Batch all proven local failures to
        # retain the existing systemic-crash policy.
        dispatch.detect_crashed_workers(conn, board=context.get('board'), failed_results_only=True)
    return True


def observe(context):
    from hermes_cli.kanban_failed_result import process_gone

    path = Path(context['db'])
    if not path.is_file():
        # A removed board has no run to reconcile. Never recreate it on restart.
        return
    with connect_existing(path) as conn:
        if conn.execute('SELECT 1 FROM tasks WHERE id=? AND current_run_id=?',
                        (context['task_id'], context['run_id'])).fetchone() is None:
            return
        # The acknowledgement precedes the writer's result transaction.
        conn.execute('PRAGMA busy_timeout=250')
        if context.get('ready_path'):
            ready = Path(context['ready_path'])
            # The unit's ExecStopPost released the directory before this restart.
            ready.parent.mkdir(exist_ok=True)
            ready.write_text(context['observer_unit'], encoding='utf-8')
        else:
            sys.stdout.buffer.write(READY)
            sys.stdout.buffer.flush()
        while True:
            # Lock-free and cheap: leave as soon as this inode is not the board at
            # its path (archived, deleted or replaced), instead of polling it forever.
            path = _generation(conn)
            if path is None:
                return
            row = conn.execute('SELECT status, current_run_id, claim_lock FROM tasks WHERE id=?',
                               (context['task_id'],)).fetchone()
            if (row is None or row['status'] != 'running' or row['current_run_id'] != context['run_id']
                    or row['claim_lock'] != context['claim_lock']):
                return
            from hermes_cli.kanban_failure_release import queued
            if queued(conn, context['run_id']):
                # A Board-primary release is durably queued: the feed, not this
                # observer, closes the run, so waiting here would outlive sync.
                return
            result = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                                  "AND kind='worker_result' LIMIT 1",
                                  (context['task_id'], context['run_id'])).fetchone()
            if result is None and process_gone(context.get('writer_pid'), context.get('writer_birth')):
                # Writer died before commit: there is no failed result for this
                # observer to reconcile. Ordinary crash recovery still owns it.
                return
            try:
                # Idle/live polls are read-only and hold no lock.
                if not _finalize(conn, context, path):
                    return
            except sqlite3.OperationalError:
                # Committed results are the queue; retry transient board locks.
                pass
            time.sleep(POLL_SECONDS)


def _release_ready(context):
    """Best-effort removal of the ``failed-result-*/ready`` acknowledgment tempdir.

    Only that exact shape is touched, so a malformed context cannot aim this at
    another path. Errors are swallowed: cleanup never changes the exit status.
    """
    try:
        ready = Path(context['ready_path'])
        if ready.name != 'ready' or not ready.parent.name.startswith('failed-result-'):
            return
        ready.unlink(missing_ok=True)
        ready.parent.rmdir()
    except (KeyError, TypeError, ValueError, OSError):
        pass


def _stop_own_unit(context):
    """[daveey] Queue a stop of this observer's own unit, so a retire is final on any unit.

    Transient units keep the properties they were submitted with: one armed before
    ``RestartPreventExitStatus`` existed restarts a deterministic retire forever.
    A pending stop job cancels the manager's auto-restart. Only a
    ``hermes-failed-result-*.service`` named by the context AND holding this process
    (``/proc/self/cgroup``) is touched. Best effort: never changes the exit status.
    """
    try:
        unit = context.get('observer_unit')
        if not (isinstance(unit, str) and unit.startswith('hermes-failed-result-') and unit.endswith('.service')):
            return
        cgroup = Path('/proc/self/cgroup').read_text(encoding='utf-8')
        if not any(line.rsplit('/', 1)[-1] == unit for line in cgroup.splitlines()):
            return
        from hermes_platform.resolver import locate_command
        systemctl = locate_command('systemctl').command
        if systemctl:
            subprocess.run([*systemctl, '--user', '--no-block', 'stop', unit], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
    except (AttributeError, ImportError, OSError, ValueError, subprocess.SubprocessError):
        pass


def _retire(context):
    if isinstance(context, dict):
        _release_ready(context)
        _stop_own_unit(context)
    return STARTUP_FAILURE_EXIT


def main(argv):
    """Run one observation; returns the process exit status.

    [daveey] An import failure (``No module named 'ruamel'`` from a wrong
    interpreter or a broken checkout) is deterministic: restarting replays it
    forever. Exit ``STARTUP_FAILURE_EXIT``, which the service lists in
    ``RestartPreventExitStatus``, so the manager retires the unit instead.
    A missing or malformed context argument is equally deterministic.
    Retiring releases the readiness tempdir, as success does, and queues a stop of
    its own unit (``_stop_own_unit``) for units armed without that exit status.
    """
    context = None
    try:
        context = json.loads(argv[1])
        if not isinstance(context, dict) or not context.get('db') or any(key not in context for key in CONTEXT_KEYS):
            raise ValueError(f'context lacks {CONTEXT_KEYS}')
    except (IndexError, TypeError, ValueError) as exc:
        print(f'failed-result observer has no usable context, retiring: {exc!r}', file=sys.stderr)
        return _retire(context)
    try:
        observe(context)
    except ImportError as exc:
        print(f'failed-result observer cannot import its code, retiring: {exc!r}', file=sys.stderr)
        return _retire(context)
    # Unexpected errors exit nonzero for manager restart; the unit's ExecStopPost
    # releases the tempdir after each stop and the restart recreates it.
    _release_ready(context)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
