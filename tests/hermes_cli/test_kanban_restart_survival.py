"""Real detached workers outlive gateway and Desktop backend group teardown."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


def wait_for(predicate):
    until = time.monotonic() + 30
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('subprocess readiness timed out')


@pytest.mark.live_system_guard_bypass  # signals only PIDs spawned by this test, intentionally orphaned
@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['gateway', 'desktop'])
def test_worker_survives_entrypoint_restart(tmp_path, monkeypatch, entry):
    node = shutil.which('node')
    if entry == 'desktop' and not node:
        pytest.skip('Desktop requires Node with TypeScript support')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    board = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(board))
    kbc.init_db(board)
    with kbc.connect_closing(board) as conn:
        tid = kb.create_task(conn, title='restart survival', assignee='default')
        kb.recompute_ready(conn)
    root = Path(__file__).resolve().parents[2]
    # Both entrypoints use _default_spawn. Replace only the LLM command with a
    # deterministic worker; keep the real environment, file FDs and setsid.
    worker_code = f'''
import sys
sys.path[:] = {sys.path!r}
from pathlib import Path
import os, time
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
root = Path({str(tmp_path)!r})
(root / 'worker-ready').write_text(str(os.getpid()))
while not (root / 'continue').exists(): time.sleep(.05)
with kbc.connect_closing(root / 'board.db') as conn:
    kb.complete_task(conn, {tid!r}, result='survived restart', expected_run_id=int(os.environ['HERMES_KANBAN_RUN_ID']))
(root / 'done').write_text('done')
'''
    parent_code = f'''
import sys
sys.path[:] = {sys.path!r}
import time
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as d
from hermes_cli import worker_compute_capacity as capacity
root = Path({str(tmp_path)!r})
assert capacity.policy_root() == root, 'child account policy must be isolated'
d._worker_argv = lambda *a: [{sys.executable!r}, '-c', {worker_code!r}]
d._retag_legacy_worker_sessions = lambda *a: None
with kbc.connect_closing(root / 'board.db') as conn:
    task = kb.claim_task(conn, {tid!r})
    pid = d._default_spawn(task, str(root))
    d._set_worker_pid(conn, task.id, pid)
(root / 'parent-ready').write_text(str(pid))
while True: time.sleep(1)
'''
    env = dict(os.environ)
    # conftest's in-process policy_root patch does not cross exec. Keep the
    # child's admission inventory away from the real worker account as well.
    env['HERMES_TEST_POLICY_ROOT'] = str(tmp_path)
    for key in list(env):
        if key.startswith('HERMES_KANBAN_') and key != 'HERMES_KANBAN_DB':
            env.pop(key)
    parent = subprocess.Popen([sys.executable, '-c', parent_code], env=env,
                              start_new_session=True, stdout=subprocess.DEVNULL,
                              stderr=(tmp_path / 'parent.log').open('w'))
    pid = None
    try:
        try:
            wait_for(lambda: (tmp_path / 'parent-ready').exists())
        except AssertionError:
            print((tmp_path / 'parent.log').read_text())
            raise
        # write_text truncates before it writes; wait for the pid, not the file.
        wait_for(lambda: (tmp_path / 'worker-ready').exists()
                 and (tmp_path / 'worker-ready').read_text().strip().isdigit())
        pid = int((tmp_path / 'worker-ready').read_text())
        assert os.getpgid(pid) != os.getpgid(parent.pid)
        with kbc.connect_closing(board) as conn:
            run_id = kb.get_task(conn, tid).current_run_id
        if entry == 'desktop':
            # Execute Desktop's actual managed-backend teardown, not a copy.
            module = (root / 'apps/desktop/electron/backend-child.ts').as_uri()
            js = f"import {{stopBackendChild}} from {json.dumps(module)}; stopBackendChild({{pid:{parent.pid},kill:s=>process.kill({parent.pid},s)}},{{forceKillProcessTree:()=>{{throw Error('unexpected')}}}});"
            subprocess.run([node, '--input-type=module', '-e', js], check=True, timeout=20)
        else:
            os.killpg(parent.pid, signal.SIGTERM)
        parent.wait(timeout=15)
        assert dispatch._pid_alive(pid)
        # A fresh dispatcher connection must preserve this same run, even when
        # its old dispatcher's lease has expired.
        with kbc.connect_closing(board) as conn:
            conn.execute('UPDATE tasks SET claim_expires=0 WHERE id=?', (tid,))
            conn.commit()
            assert kb.release_stale_claims(conn) == 0
            assert dispatch.detect_crashed_workers(conn) == []
            assert kb.get_task(conn, tid).current_run_id == run_id
        (tmp_path / 'continue').touch()
        wait_for(lambda: (tmp_path / 'done').exists())
        with kbc.connect_closing(board) as conn:
            assert kb.get_task(conn, tid).status == 'done'
            row = conn.execute('SELECT outcome FROM task_runs WHERE id=?', (run_id,)).fetchone()
            assert row[0] == 'completed'
    finally:
        if parent.poll() is None:
            os.killpg(parent.pid, signal.SIGKILL)
            parent.wait(timeout=5)
        if pid and dispatch._pid_alive(pid):
            os.kill(pid, signal.SIGKILL)
