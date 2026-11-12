"""A disposable proof-home backend keeps recovering its OWN persisted job across SIGTERM/restart.

Real ``hermes serve`` processes, all owned by this test: the sentinel is a production-shaped
``$HOME/.hermes`` holding an unrelated recoverable chat, a dead-writer turn marker and a persisted
job; the backend runs as an agent tool child with ``--proof-home``. Only fixture PIDs are signalled.
"""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from tests.hermes_cli.test_serve_proof_home import ROOT, _clean_env, _snapshot, make_sentinel
from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux")  # host start-time identity reads /proc

BOOT_S = 120.0


def _start_time(pid):
    from tools.process_registry import ProcessRegistry

    return ProcessRegistry._safe_host_start_time(pid)


def _adopted_by(proof, pid):
    try:
        entries = json.loads((proof / 'processes.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return any(e.get('session_id') == 'proc_proof_fixture' and e.get('writer_pid') == pid for e in entries)


def _serve(proof, env):
    return subprocess.Popen([sys.executable, '-m', 'hermes_cli.main', 'serve', '--proof-home', str(proof),
                             '--isolated', '--host', '127.0.0.1', '--port', '0'],
                            env=env, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def _stop(proc):
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)


def test_proof_home_backend_recovers_own_job_through_restart(tmp_path):
    sentinel = make_sentinel(tmp_path)
    proof = tmp_path / 'proof'
    proof.mkdir()
    job = subprocess.Popen(['sleep', '600'], start_new_session=True)  # the proof's own persisted job
    gone = subprocess.Popen([sys.executable, '-c', 'pass'])
    gone.wait(timeout=5)
    (proof / 'processes.json').write_text(json.dumps([{
        'session_id': 'proc_proof_fixture', 'command': 'sleep 600', 'pid': job.pid, 'pid_scope': 'host',
        'host_start_time': _start_time(job.pid), 'started_at': time.time(), 'cwd': str(proof),
        'session_key': '20261112_000000_proof', 'persist_on_release': True, 'notify_on_complete': False,
        'writer_pid': gone.pid,
    }]))
    before = _snapshot(sentinel)
    env = _clean_env(sentinel, 'HERMES_AGENT_TOOL_CALL', {
        'HERMES_AGENT_HOME': str(sentinel), 'HERMES_DESKTOP': '1',
        'HERMES_DASHBOARD_SESSION_TOKEN': 'proof-fixture-token'})
    backends = []
    try:
        for _ in range(2):  # boot, SIGTERM, boot again
            backends.append(_serve(proof, env))
            proc = backends[-1]
            wait_until(lambda: _adopted_by(proof, proc.pid) or proc.poll() is not None, timeout=BOOT_S,
                       interval=0.5, what='proof backend re-adopting its own job')
            assert proc.poll() is None, f'proof backend exited {proc.returncode}'
            assert job.poll() is None, 'the adopted job must survive its backend'
            _stop(proc)
        assert _snapshot(sentinel) == before
    finally:
        for proc in backends:
            _stop(proc)
        job.kill()
        job.wait(timeout=10)
