"""Portable model evidence is authored by the admitting host, not the reader."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_completion_refs_sync import replay


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    with kbc.connect(home / 'worker.db') as conn, kbc.connect(home / 'peer.db') as peer:
        tid = kb.create_task(conn, title='model evidence', assignee='dev')
        run = kb.claim_task(conn, tid).current_run_id
        now = int(time.time())
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL)')
            db.execute('CREATE TABLE session_model_usage (session_id TEXT, model TEXT)')
            db.execute('INSERT INTO sessions VALUES (?, ?, ?)', ('session', 'class:deep', now))
            db.executemany('INSERT INTO session_model_usage VALUES (?, ?)',
                           [('session', 'model-b'), ('session', 'model-a'), ('session', '')])
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'owner_admitted',
                             {'session_id': 'session', 'db': str(home / 'state.db')}, run_id=run)
        yield conn, peer, tid, run, home


@pytest.mark.parametrize('kind', ['completed', 'blocked', 'continued', 'review_requested', 'scheduled',
                                  'dependency_wait', 'worker_result', 'crashed', 'timed_out', 'reclaimed',
                                  'stale', 'reconciled', 'protocol_violation', 'rate_limited',
                                  'owner_restart_lost', 'owner_unavailable', 'host_reboot',
                                  'oom_killed', 'signaled'])
def test_terminal_evidence_survives_board_replay(admitted, kind):
    conn, peer, tid, run, _ = admitted
    with kb.write_txn(conn):
        kb._append_event(conn, tid, kind, {'reason': 'fixture'}, run_id=run)
    replay(conn, peer)
    row = peer.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?', (tid, kind)).fetchone()
    assert json.loads(row[0])['session_models'] == ['model-a', 'model-b']


@pytest.mark.parametrize('bad', ['foreign', 'path', 'time', 'missing', 'extra_admission'])
def test_unknown_or_foreign_evidence_is_omitted(admitted, bad):
    conn, _, tid, run, home = admitted
    with kb.write_txn(conn):
        if bad == 'foreign':
            conn.execute("UPDATE task_events SET origin_gateway='elsewhere' WHERE kind='owner_admitted'")
        elif bad == 'path':
            conn.execute("UPDATE task_events SET payload=? WHERE kind='owner_admitted'",
                         (json.dumps({'session_id': 'session', 'db': str(home / 'copy.db')}),))
        elif bad == 'extra_admission':
            kb._append_event(conn, tid, 'owner_admitted',
                             {'session_id': 'absent', 'db': str(home / 'state.db')}, run_id=run)
        else:
            with sqlite3.connect(home / 'state.db') as db:
                db.execute('UPDATE sessions SET started_at=0' if bad == 'time' else 'DELETE FROM sessions')
        kb._append_event(conn, tid, 'continued', {'session_models': ['untrusted']}, run_id=run)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='continued'").fetchone()
    assert 'session_models' not in json.loads(row[0])


@pytest.mark.parametrize('action,kind', [('continue', 'continued'), ('review', 'review_requested'),
                                        ('dependency', 'dependency_wait')])
def test_real_lifecycle_transition_carries_models(admitted, action, kind):
    from hermes_cli.kanban_continuation import continue_task
    conn, peer, tid, run, _ = admitted
    if action == 'continue':
        assert continue_task(conn, tid, checkpoint='continue implementation', expected_run_id=run)
    elif action == 'review':
        assert kb.request_review(conn, tid, summary='verified implementation', expected_run_id=run)
    else:
        parent = kb.create_task(conn, title='prerequisite implementation', assignee='dev')
        kb.link_tasks(conn, parent, tid, expected_child_run_id=run)
        assert kb.block_task(conn, tid, kind='dependency', reason='prerequisite', expected_run_id=run)
    replay(conn, peer)
    row = peer.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?', (tid, kind)).fetchone()
    assert json.loads(row[0])['session_models'] == ['model-a', 'model-b']


@pytest.mark.parametrize('status', ['done', 'blocked', 'archived'])
def test_historical_attestation_does_not_reset_wake_episode(admitted, status):
    from hermes_cli.kanban_run_models_cli import attest
    from hermes_cli import kanban_db_sync as sync
    conn, _, tid, _, _ = admitted
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
        conn.execute('INSERT INTO task_wake_episodes(task_id, cmd, first_at, checks) VALUES (?, ?, ?, ?)',
                     (tid, 'true', 123, 2))
    before = list(conn.execute('SELECT * FROM task_wake_episodes'))
    count = conn.execute('SELECT count(*) FROM task_events').fetchone()[0]
    outbox = len(sync.outbox(conn))
    assert attest(conn, tid)[0]['status'] == 'attested'
    assert list(conn.execute('SELECT * FROM task_wake_episodes')) == before
    assert conn.execute('SELECT count(*) FROM task_events').fetchone()[0] == count + 1
    assert len(sync.outbox(conn)) == outbox + 1


def test_attestation_is_local_readonly_and_idempotent(admitted):
    from hermes_cli.kanban_run_models_cli import attest
    conn, peer, tid, run, home = admitted
    before = (home / 'state.db').read_bytes()
    first = attest(conn, tid)
    second = attest(conn, tid)
    assert first[0]['status'] == 'attested'
    assert second[0]['status'] == 'unchanged'
    assert first[0]['session_models'] == ['model-a', 'model-b']
    assert first[0]['run_uid'] == conn.execute('SELECT uid FROM task_runs WHERE id=?', (run,)).fetchone()[0]
    assert (home / 'state.db').read_bytes() == before
    replay(conn, peer, origin='different-host')
    assert attest(peer, tid)[0]['status'] == 'unknown_or_foreign'
    assert conn.execute("SELECT count(*) FROM task_events WHERE kind='run_models_attested'").fetchone()[0] == 1


def test_evicted_copy_is_hydrated_without_republishing_history(admitted, monkeypatch):
    from hermes_cli import kanban_worker_node as node
    from hermes_cli.kanban_run_models_cli import attest
    from hermes_cli import kanban_db_sync as sync
    conn, replica, tid, _, _ = admitted
    gateway = conn.execute('SELECT gateway_id FROM kanban_sync_state').fetchone()[0]
    replay(conn, replica, origin=gateway)
    # Keep the durable replica open while exercising the real import path.
    class Replica:
        def __getattr__(self, name):
            return getattr(replica, name)
        def close(self):
            pass
    monkeypatch.setattr(node, 'open_replica', lambda _: Replica())
    with kb.write_txn(conn):
        conn.execute('UPDATE kanban_sync_state SET applying=1')
        conn.execute('UPDATE tasks SET current_run_id=NULL WHERE id=?', (tid,))
        conn.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
        conn.execute('DELETE FROM task_runs WHERE task_id=?', (tid,))
        conn.execute('DELETE FROM tasks WHERE id=?', (tid,))
        conn.execute('UPDATE kanban_sync_state SET applying=0')
    before = len(sync.outbox(conn))
    rows = attest(conn, tid)
    assert rows[0]['status'] == 'attested'
    assert len(sync.outbox(conn)) == before + 1
    assert attest(conn, tid)[0]['status'] == 'unchanged'


def test_cli_parser_and_command_attest_admitted_models(admitted, monkeypatch, capsys):
    import argparse
    from contextlib import contextmanager
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    from hermes_cli.kanban_run_models_cli import command
    conn, peer, tid, run, _ = admitted
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    args = parser.parse_args(['kanban', 'attest-run-models', tid, '--json'])
    assert args.kanban_action == 'attest-run-models' and args.task_ids == [tid] and args.json
    assert kanban._HANDLERS['attest-run-models'] is command
    @contextmanager
    def opened(*_a, **_k):
        yield conn
    monkeypatch.setattr('hermes_cli.kanban_run_models_cli.kbc.connect', opened)
    assert command(args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]['status'] == 'attested' and rows[0]['session_models'] == ['model-a', 'model-b']
    assert rows[0]['run_uid'] == conn.execute('SELECT uid FROM task_runs WHERE id=?', (run,)).fetchone()[0]
    replay(conn, peer)
    row = peer.execute("SELECT payload FROM task_events WHERE kind='run_models_attested'").fetchone()
    assert json.loads(row[0])['session_models'] == ['model-a', 'model-b']


def test_dead_run_finalizer_stamps_models_through_replay(admitted):
    import subprocess, sys
    from hermes_cli.kanban_db_dispatch_dead_worker import _DeadWorker
    from hermes_cli.kanban_run_finalizer import close_dead_run
    conn, peer, tid, run, _ = admitted
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=? WHERE id=?', (proc.pid, tid))
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    dead = _DeadWorker('unknown', None, f'pid {proc.pid} not alive', 'crashed',
                       {'pid': proc.pid, 'claimer': row['claim_lock']})
    with kb.write_txn(conn):
        assert close_dead_run(conn, row, dead) is not None
    replay(conn, peer)
    payload = json.loads(peer.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?',
                                      (tid, 'crashed')).fetchone()[0])
    assert payload['session_models'] == ['model-a', 'model-b']
    assert payload['pid'] == proc.pid
    assert conn.execute('SELECT ended_at FROM task_runs WHERE id=?', (run,)).fetchone()[0] is not None


def test_evidence_never_waits_on_tracking_reservation(admitted):
    """A held tracking reservation must omit evidence, not park the board write transaction."""
    import threading
    from hermes_cli import kanban_run_models as models
    from hermes_cli import sqlite_offline_reservations as offline
    _, _, tid, run, home = admitted
    finished, reserved, result = threading.Event(), threading.Event(), []

    def hold():  # A different thread owns the reservation, so a waiter would block on `finished`.
        offline.reserve(offline.database_key(home / 'state.db'), finished)
        reserved.set()
        finished.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert reserved.wait(5)
    def read():  # sqlite connections are thread-bound: reopen the board in the reader thread.
        with kbc.connect(home / 'worker.db') as own:
            assert models.local_gateway(own)
            result.append(models.evidence(own, tid, run))

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(3)
    blocked = worker.is_alive()
    finished.set()  # Release before asserting so a RED run does not leak a blocked thread.
    worker.join(5)
    holder.join(5)
    assert not blocked, 'evidence() waited for the SQLite tracking reservation'
    assert result == [None]
    with kbc.connect(home / 'worker.db') as own:  # Released reservation: evidence is available again.
        assert models.evidence(own, tid, run)['session_models'] == ['model-a', 'model-b']


def test_evidence_opens_state_db_without_waiting_for_tracking(admitted, monkeypatch):
    from hermes_cli import kanban_run_models as models
    conn, _, tid, run, _ = admitted
    seen = {}
    real = models.connect_tracked

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(models, 'connect_tracked', spy)
    assert models.evidence(conn, tid, run)['session_models'] == ['model-a', 'model-b']
    assert seen.get('wait_for_tracking') is False


def _stamped(conn, tid, run, home, *, run_start, run_end, admitted, session_start, names=None):
    """Pin every clock involved, then ask for evidence on a fresh terminal event."""
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('UPDATE sessions SET started_at=? WHERE id=?', (session_start, 'session'))
        if names is not None:
            db.execute('DELETE FROM session_model_usage WHERE session_id=?', ('session',))
            db.executemany('INSERT INTO session_model_usage VALUES (?, ?)', [('session', n) for n in names])
    with kb.write_txn(conn):
        conn.execute('UPDATE task_runs SET started_at=?, ended_at=? WHERE id=?', (run_start, run_end, run))
        conn.execute("UPDATE task_events SET created_at=? WHERE kind='owner_admitted'", (admitted,))
        kb._append_event(conn, tid, 'continued', {'session_models': ['untrusted']}, run_id=run)
    row = conn.execute("SELECT payload FROM task_events WHERE kind='continued'").fetchone()
    return json.loads(row[0]).get('session_models')


@pytest.mark.parametrize('offset,expected', [(0.999, ['model-a', 'model-b']), (1, None)])
def test_session_must_start_before_admission_plus_one_second(admitted, offset, expected):
    conn, _, tid, run, home = admitted
    got = _stamped(conn, tid, run, home, run_start=1000, run_end=None, admitted=2000,
                   session_start=2000 + offset)
    assert got == expected


@pytest.mark.parametrize('offset,expected', [(0.999, ['model-a', 'model-b']), (1, None)])
def test_session_must_start_before_run_end_plus_one_second(admitted, offset, expected):
    conn, _, tid, run, home = admitted
    got = _stamped(conn, tid, run, home, run_start=1000, run_end=1500, admitted=5000,
                   session_start=1500 + offset)
    assert got == expected


@pytest.mark.parametrize('admitted_at,expected', [(1000, ['model-a', 'model-b']), (999.5, None)])
def test_admission_must_not_precede_run_start(admitted, admitted_at, expected):
    conn, _, tid, run, home = admitted
    # The session window [run start, admission + 1) stays non-empty, so only this guard can refuse.
    got = _stamped(conn, tid, run, home, run_start=1000, run_end=None, admitted=admitted_at,
                   session_start=1000.25)
    assert got == expected


@pytest.mark.parametrize('names', [[], [''], ['   ', 'class:fast'], ['CLASS: deep']])
def test_only_selectors_or_blank_names_grant_no_models(admitted, names):
    conn, _, tid, run, home = admitted
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("UPDATE sessions SET model='class:deep' WHERE id=?", ('session',))
    got = _stamped(conn, tid, run, home, run_start=1000, run_end=None, admitted=1000,
                   session_start=1000, names=names)
    assert got is None
