"""Real supervisor dispatcher drain with populated replicas and descriptor pressure.

Synthetic scale replay, not a hub P1 acceptance result. Includes every drain/poll/
apply_incoming path; no callback is stubbed. Network and process attempts fail the
run even when production catches their exceptions. Run in the canonical test env.
"""
import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch


def measure(live, replica, ticks, retained):
    from hermes_cli import kanban_foreign_card as foreign
    from hermes_cli import kanban_replica_reader as readers
    from hermes_cli import kanban_reader_retention as retention
    from hermes_cli import kanban_supervisor_requests as supervisor
    from hermes_cli import kanban_operator_call as operator
    from hermes_cli import kanban_sweep_gate as gate
    from hermes_cli import kanban_lockguard as guard

    path = Path(replica.execute('PRAGMA database_list').fetchone()[2])
    clock = [1800000000]
    forbidden, opens = [], []
    original = guard.connect

    def counted(*args, **kwargs):
        opens.append(True)
        return original(*args, **kwargs)

    def refuse(*args, **kwargs):
        forbidden.append(True)
        raise AssertionError('replay attempted external side effect')

    retention.close_all()
    gate.reset()
    live.execute('DELETE FROM supervisor_executors')
    live.commit()
    with ExitStack() as stack:
        stack.enter_context(patch.object(foreign, 'replica_path', lambda _: path))
        stack.enter_context(patch.object(operator, '_own_gateway', lambda _: 'replay-home'))
        stack.enter_context(patch.object(operator, '_board_slug', lambda _: 'default'))
        stack.enter_context(patch.object(socket.socket, 'connect', refuse))
        stack.enter_context(patch.object(subprocess, 'Popen', refuse))
        stack.enter_context(patch.object(supervisor.time, 'time', lambda: clock[0]))
        stack.enter_context(patch.object(guard, 'connect', counted))
        warnings = stack.enter_context(patch.object(supervisor._log, 'warning'))
        if not retained:
            stack.enter_context(patch.object(readers, 'open_replica', foreign.open_replica))
        supervisor.dispatcher_drain(live)
        assert not warnings.called, warnings.call_args_list
        assert live.execute('SELECT count(*) FROM operator_call_scan').fetchone()[0] > 0
        opens.clear()
        cpu = 0.0
        for tick in range(ticks):
            clock[0] += 15
            # External commits must remain visible, though no operator action is queued.
            if tick % 3 == 0:
                replica.execute('UPDATE tasks SET title=? WHERE id=?', (str(tick), 't_0000000000000000'))
                replica.commit()
            start = time.thread_time()
            result = supervisor.dispatcher_drain(live)
            cpu += time.thread_time() - start
            assert result == []
            assert not warnings.called, warnings.call_args_list
        assert not forbidden, 'caught network/process attempt invalidates replay'
    retention.close_all()
    return {'cpu_ms_per_tick': 1000 * cpu / ticks, 'core_percent_nine_boards': 900 * cpu / (15 * ticks),
            'guarded_opens': len(opens), 'ticks': ticks,
            'last_seen': live.execute('SELECT MAX(last_seen) FROM supervisor_executors').fetchone()[0]}


def run(home, tasks, replicas, ticks, wal_mb, fds):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_supervisor_requests as supervisor

    with ExitStack() as stack:
        live = stack.enter_context(closing(kbc.connect(home / 'live.db')))
        rep = stack.enter_context(closing(kbc.connect(home / 'replica.db')))
        for _ in range(fds):
            stack.enter_context(open(os.devnull, 'rb'))
        for conn, size in ((live, tasks), (rep, replicas)):
            for table in ('task_events', 'task_runs'):
                if 'uid' not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN uid TEXT')
            conn.commit()
            template = kb.create_task(conn, title='replay fixture', assignee='default', created_by='default')
            cols = [r[1] for r in conn.execute('PRAGMA table_info(tasks)') if r[1] != 'id']
            values = tuple(conn.execute(f"SELECT {','.join(cols)} FROM tasks WHERE id=?", (template,)).fetchone())
            conn.execute('BEGIN')
            conn.executemany(f"INSERT INTO tasks(id,{','.join(cols)}) VALUES ({','.join('?' for _ in range(len(cols)+1))})",
                             ((f't_{i:016x}', *values) for i in range(size)))
            conn.commit()
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        supervisor.ensure_schema(live)
        live.commit()
        rep.execute('PRAGMA wal_autocheckpoint=0')
        if wal_mb:
            rep.execute('CREATE TABLE wal_padding(payload BLOB)')
            for _ in range(wal_mb):
                rep.execute('INSERT INTO wal_padding VALUES (zeroblob(1048576))')
                rep.commit()
        wal = Path(str(home / 'replica.db') + '-wal')
        return {'tasks': tasks, 'replica_tasks': replicas, 'extra_fds': fds,
                'wal_bytes': wal.stat().st_size if wal.exists() else 0,
                'legacy': measure(live, rep, ticks, False), 'retained': measure(live, rep, ticks, True)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ticks', type=int, default=240)
    parser.add_argument('--wal-mb', type=int, default=0)
    parser.add_argument('--fds', type=int, default=512)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.ticks < 1 or min(args.wal_mb, args.fds) < 0:
        parser.error('positive ticks and nonnegative WAL/FD counts required')
    with tempfile.TemporaryDirectory(prefix='replica-drain-replay-') as root:
        os.environ['HERMES_HOME'] = root
        for key in tuple(os.environ):
            if key.startswith(('HERMES_KANBAN', 'HERMES_SESSION', 'HERMES_DELEGATED_CHILD_CONTEXT')):
                del os.environ[key]
        results = []
        for tasks, replicas in ((1000, 10000), (5800, 40000), (20000, 40000)):
            home = Path(root) / str(tasks)
            home.mkdir()
            results.append(run(home, tasks, replicas, args.ticks, args.wal_mb, args.fds))
    text = json.dumps(results, indent=2)
    args.output.write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
