"""Independent board-only failed-result observer. [daveey]

Pre-armed before the result commit, so a dying writer cannot strand a committed
result in a commit-to-launch gap. Never reads the owner's session store.
"""
from __future__ import annotations

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


def arm(context):
    """Wait for a detached observer to open this board before recording failure.

    Scope-unavailable/startup errors are reported by the caller, not hidden as
    a successful arm. No model/provider credentials are needed by the child.
    """
    from tools.environments.local import build_subprocess_env
    from tools.process_registry import restart_safe_gateway_child_argv, systemd_user_bus_env
    from hermes_cli.kanban_db_dispatch import _process_fingerprint

    context = {key: context[key] for key in ('db', 'task_id', 'run_id', 'claim_lock', 'board') if key in context}
    context.update(writer_pid=os.getpid(), writer_birth=_process_fingerprint(os.getpid()))
    command = [sys.executable, '-m', __name__, json.dumps(context)]
    launch = restart_safe_gateway_child_argv(
        command, unit_suffix=f'failed-result-{os.getpid()}-{time.monotonic_ns()}',
        require_restart_safe_scope=True, outlives_parent=True)
    if launch.mode == 'degraded':
        raise RuntimeError('independent failed-result observer scope unavailable')
    proc = subprocess.Popen(
        launch.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
        env=build_subprocess_env(extra=systemd_user_bus_env()),
        cwd=str(Path(__file__).resolve().parents[1]),
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


def observe(context):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_failed_result import failed_result_proof, process_gone

    with connect_closing(Path(context['db'])) as conn:
        # The acknowledgement precedes the writer's result transaction.
        sys.stdout.buffer.write(READY)
        sys.stdout.buffer.flush()
        while True:
            row = conn.execute('SELECT status, current_run_id, claim_lock FROM tasks WHERE id=?',
                               (context['task_id'],)).fetchone()
            if (row is None or row['status'] != 'running' or row['current_run_id'] != context['run_id']
                    or row['claim_lock'] != context['claim_lock']):
                return
            result = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? "
                                  "AND kind='worker_result' LIMIT 1",
                                  (context['task_id'], context['run_id'])).fetchone()
            if result is None and process_gone(context.get('writer_pid'), context.get('writer_birth')):
                # Writer died before commit: there is no failed result for this
                # observer to reconcile. Ordinary crash recovery still owns it.
                return
            try:
                # Idle/live polls are read-only. Revalidate under the sweep's
                # write transaction before any closure; a competing dispatcher
                # may have ended/replaced this claim meanwhile. Batch all proven
                # local failures to retain the existing systemic-crash policy.
                if failed_result_proof(conn, context['task_id'], context['run_id'], context['claim_lock']):
                    dispatch.detect_crashed_workers(conn, board=context.get('board'), failed_results_only=True)
            except sqlite3.OperationalError:
                # Committed results are the queue; retry transient board locks.
                pass
            time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    observe(json.loads(sys.argv[1]))
