"""Expiry must still terminate verified workers after they publish a failed result."""
from __future__ import annotations

import json
import select
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures
from tools.environments.local import build_subprocess_env

board = fixtures.board

#: Stand-in instantiation epoch where the host has none to read. Strict death proof needs a
#: nonempty epoch (``valid_birth``), and ``current_instantiation_epoch`` is empty off Linux (macOS
#: has no boot_id/PID-1 start in /proc). Production deliberately keeps such hosts out of the strict
#: protocol (test_empty_epoch_host_never_records_strict_death_proof); these tests model the
#: protocol itself with real processes and signals, so they pin one epoch in parent AND writer.
_FIXTURE_EPOCH = 'fixture-runtime-epoch'
_EPOCH_ENV = 'HERMES_TEST_FIXTURE_INSTANTIATION_EPOCH'
_PIN_CHILD_EPOCH = (
    'import os as _fixture_os\n'
    'import gateway.drain_control as _fixture_drain_control\n'
    f'_fixture_epoch = _fixture_os.environ[{_EPOCH_ENV!r}]\n'
    '_fixture_drain_control.current_instantiation_epoch = lambda: _fixture_epoch\n')


@pytest.fixture(autouse=True)
def instantiation_epoch(monkeypatch):
    """The host's real epoch (Linux), else the fixture's; the writer child is pinned to the same."""
    from gateway import drain_control
    epoch = drain_control.current_instantiation_epoch() or _FIXTURE_EPOCH
    monkeypatch.setattr(drain_control, 'current_instantiation_epoch', lambda: epoch)
    return epoch


@contextmanager
def _hung_writer(conn, context, *, code=1, output='fixture worker_receipt_timeout', outer_pid=None,
                 strict=True):
    from gateway import drain_control
    script = _PIN_CHILD_EPOCH + fixtures._CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        f'_record_worker_result(context, {code!r}, {output!r})').replace(
        'raise SystemExit(1)', "print('RESULT_COMMITTED', flush=True)\nsys.stdin.read(1)")
    proc = subprocess.Popen(
        [sys.executable, '-c', script, json.dumps(context), 'unit'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8',
        env={**build_subprocess_env(), _EPOCH_ENV: drain_control.current_instantiation_epoch()},
        cwd=str(Path(__file__).resolve().parents[2]))
    try:
        kbd._set_worker_pid(conn, context['task_id'], outer_pid or proc.pid)
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write('x')
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 20)
        assert ready, 'writer did not commit its result'
        assert proc.stdout.readline().strip() == 'RESULT_COMMITTED'
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind='worker_result'",
            (context['run_id'],)).fetchone()[0])
        assert payload.get('death_proof_version') == (1 if strict else None), payload
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def _expire(conn, context, entry):
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET claim_expires=?, last_heartbeat_at=1, max_runtime_seconds=? WHERE id=?',
                     (1 if entry == 'ttl' else 4000000000, 1 if entry == 'max_runtime' else None,
                      context['task_id']))
        conn.execute('UPDATE task_runs SET started_at=1 WHERE id=?', (context['run_id'],))


def _enforce(conn, entry, **kwargs):
    return (kb.release_stale_claims if entry == 'ttl' else kbd.enforce_max_runtime)(conn, **kwargs)


def _assert_open(conn, context):
    run = conn.execute('SELECT status, ended_at FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert tuple(run) == ('running', None)
    assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                        (context['task_id'],)).fetchone()[0] == 0


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
def test_failed_result_expiry_terminates_but_only_strict_finalizer_accounts(board, entry):
    conn, context = board
    # An older binding is also hung: expiry terminates it too, but still must not
    # close/account; only the strict finalizer does, once every witness is gone.
    historical = _idle()
    try:
        _bind(conn, context, historical)
        with _hung_writer(conn, context) as writer:
            _expire(conn, context, entry)
            _enforce(conn, entry)
            assert writer.poll() is not None, 'expired verified writer was left alive'
            assert historical.poll() is not None, 'hung historical bound was left alive'
            _assert_open(conn, context)
            for _ in range(2):
                _enforce(conn, entry)
            _assert_open(conn, context)
            assert kbd.detect_crashed_workers(conn, failed_results_only=True) == [context['task_id']]
            for _ in range(2):
                _enforce(conn, entry)
                assert kbd.detect_crashed_workers(conn) == []
            assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                                (context['task_id'],)).fetchone()[0] == 1
            kinds = [row[0] for row in conn.execute('SELECT kind FROM task_events WHERE run_id=?',
                                                   (context['run_id'],))]
            assert kinds.count('crashed') == 1
            assert 'timed_out' not in kinds and 'reclaimed' not in kinds
    finally:
        _reap(historical)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
@pytest.mark.parametrize('outer_dead', [False, True])
def test_expiry_terminates_distinct_hung_outer_and_bound(board, entry, outer_dead):
    conn, context = board
    outer = _idle()
    try:
        kbd._set_worker_pid(conn, context['task_id'], outer.pid)
        with _hung_writer(conn, context, outer_pid=outer.pid) as bound:
            assert bound.pid != outer.pid
            if outer_dead:  # real outer died; the hung bound must still be reaped
                _reap(outer)
            _expire(conn, context, entry)
            _enforce(conn, entry)
            assert outer.poll() is not None
            assert bound.poll() is not None, 'hung bound process survived expiry'
            _assert_open(conn, context)
            assert kbd.detect_crashed_workers(conn, failed_results_only=True) == [context['task_id']]
    finally:
        _reap(outer)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
@pytest.mark.parametrize('bad', ['recycled', 'unknown', 'malformed'])
def test_expiry_signals_nothing_when_any_historical_witness_unverified(board, entry, bad):
    conn, context = board
    stranger = _idle()
    try:
        birth = {'recycled': 'fixture-old-epoch|1', 'unknown': None, 'malformed': 'junk'}[bad]
        with kb.write_txn(conn):
            kb._append_event(conn, context['task_id'], 'worker_bound',
                             {'pid': stranger.pid, 'started_at': birth,
                              'claim_lock': context['claim_lock']}, run_id=context['run_id'])
        with _hung_writer(conn, context) as writer:
            _expire(conn, context, entry)
            signals = []
            _enforce(conn, entry, signal_fn=lambda pid, sig: signals.append((pid, sig)))
            assert signals == []
            _enforce(conn, entry)
            assert writer.poll() is None and stranger.poll() is None
            _assert_open(conn, context)
    finally:
        _reap(stranger)


def _idle():
    return subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                            stdin=subprocess.PIPE, env=build_subprocess_env())


def _bind(conn, context, proc):
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_bound',
                         {'pid': proc.pid, 'started_at': kbd._process_fingerprint(proc.pid),
                          'claim_lock': context['claim_lock']}, run_id=context['run_id'])


def _reap(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)
    if proc.stdin is not None:
        proc.stdin.close()


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
@pytest.mark.parametrize('birth', ['recycled', None, 'UNVERIFIED', 'malformed'])
def test_failed_result_expiry_never_signals_unverified_identity(board, entry, birth):
    conn, context = board
    with _hung_writer(conn, context) as writer:
        _expire(conn, context, entry)
        # A real occupied PID with a different saved incarnation is a recycled
        # PID fixture; no host process discovery or non-fixture signals needed.
        saved = 'fixture-old-epoch|1' if birth == 'recycled' else birth
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET worker_started_at=? WHERE id=?', (saved, context['task_id']))
        signals = []
        _enforce(conn, entry, signal_fn=lambda pid, sig: signals.append((pid, sig)))
        assert signals == []
        assert writer.poll() is None
        assert kbd.detect_crashed_workers(conn, failed_results_only=True) == []
        _assert_open(conn, context)


@pytest.mark.platforms('posix')
@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
@pytest.mark.parametrize('code,output,event', [
    (76, 'owner restart fixture', 'owner_restart_lost'),
    (kb.KANBAN_RATE_LIMIT_EXIT_CODE, 'rate limit fixture', 'rate_limited'),
    (1, 'Worker failed: WorkerPersistenceError: storage_unavailable', 'owner_unavailable'),
])
def test_expiry_preserves_neutral_receipt_accounting(board, entry, code, output, event):
    conn, context = board
    with _hung_writer(conn, context, code=code, output=output) as writer:
        _expire(conn, context, entry)
        _enforce(conn, entry)
        assert writer.poll() is not None
        _assert_open(conn, context)
        assert kbd.detect_crashed_workers(conn, failed_results_only=True) == []
        assert conn.execute('SELECT ended_at FROM task_runs WHERE id=?',
                            (context['run_id'],)).fetchone()[0] is not None
        for _ in range(2):
            _enforce(conn, entry)
            assert kbd.detect_crashed_workers(conn) == []
        assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                            (context['task_id'],)).fetchone()[0] == 0
        kinds = [row[0] for row in conn.execute('SELECT kind FROM task_events WHERE run_id=?',
                                               (context['run_id'],))]
        assert kinds.count(event) == 1, kinds
        assert not {'timed_out', 'reclaimed', 'crashed'}.intersection(kinds)


@pytest.mark.platforms('posix')
def test_empty_epoch_host_never_records_strict_death_proof(board, monkeypatch):
    """A host with no instantiation epoch (macOS in production) cannot prove a reboot did not
    recycle a PID, so its writer never opts into the strict protocol or claims a verified death."""
    from gateway import drain_control
    conn, context = board
    monkeypatch.setattr(drain_control, 'current_instantiation_epoch', lambda: '')
    with _hung_writer(conn, context, strict=False) as writer:
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE run_id=? AND kind='worker_result'",
            (context['run_id'],)).fetchone()[0])
        assert payload['exit_code'] == 1 and 'result_role' not in payload, payload
        assert kbd.detect_crashed_workers(conn, failed_results_only=True) == []
        assert writer.poll() is None
        _assert_open(conn, context)
