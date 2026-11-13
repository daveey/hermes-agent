"""Idle watchers avoid repeating work without hiding new inputs or live workers."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_guard_notice as guards
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_worker_node as node


@pytest.fixture
def board(tmp_path):
    conn = kbc.connect(tmp_path / 'board.db')
    try:
        yield conn
    finally:
        conn.close()


def test_registry_failure_waits_for_ttl_and_recovers(monkeypatch):
    clock = SimpleNamespace(monotonic=lambda: 100.0)
    monkeypatch.setattr(node, 'time', clock)
    fetch = Mock(side_effect=OSError('offline'))
    node.set_registry_fetch(fetch)
    try:
        for _ in range(20):
            assert node.registry() is None
        assert fetch.call_count == 1
        clock.monotonic = lambda: 100.0 + node.REGISTRY_RETRY_S
        fetch.side_effect = None
        fetch.return_value = {'me': 'local', 'machines': [{'name': 'new'}]}
        assert 'new' in node.registry()['machines']
        assert fetch.call_count == 2
        recovered_at = clock.monotonic()
        clock.monotonic = lambda: recovered_at + node.REGISTRY_RETRY_S
        assert 'new' in node.registry()['machines']
        assert fetch.call_count == 2
        clock.monotonic = lambda: recovered_at + node.REGISTRY_TTL_S
        assert 'new' in node.registry()['machines']
        assert fetch.call_count == 3
    finally:
        node.set_registry_fetch(None)


def test_notify_count_reuses_reader_and_sees_external_commit(board, monkeypatch):
    from hermes_cli import kanban_lockguard
    path = board.execute('PRAGMA database_list').fetchone()[2]
    real = kanban_lockguard.connect
    opened = Mock(wraps=real)
    monkeypatch.setattr(kanban_lockguard, 'connect', opened)
    for _ in range(20):
        assert notify.count_notify_subs(db_path=Path(path)) == 0
    assert opened.call_count <= 1
    task = kb.create_task(board, title='new subscription', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='tui', chat_id='chat')
    assert notify.count_notify_subs(db_path=Path(path)) == 1


def test_notify_idle_reader_retention_is_bounded_and_missing_path_closes(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_reader_retention as retention
    monkeypatch.setattr(retention, 'LIMIT', 2)
    for entry in list(activity._RO_CACHE.values()):
        entry[0].close()
    activity._RO_CACHE.clear()
    for i in range(4):
        path = tmp_path / f'empty-{i}.db'
        conn = kbc.connect(path)
        conn.close()
        assert notify.count_notify_subs(db_path=path) == 0
        assert len(activity._RO_CACHE) <= 2
    entry = activity._RO_CACHE[str(path.resolve())]
    path.unlink()
    assert notify.count_notify_subs(db_path=path) == 0
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        entry[0].execute('SELECT 1')


def test_notifier_scan_filters_idle_and_disconnected_subscriptions(board):
    task = kb.create_task(board, title='notify', assignee='default')
    for platform in ('tui', 'telegram'):
        notify.add_notify_sub(board, task_id=task, platform=platform, chat_id='chat')
    assert notify.list_notify_subs(board, platforms={'telegram'}, pending_only=True) == []
    with kbc.write_txn(board):
        kb._append_event(board, task, 'completed', {})
    subs = notify.list_notify_subs(board, platforms={'TELEGRAM'}, pending_only=True)
    assert [s['platform'] for s in subs] == ['telegram']
    assert notify.list_notify_subs(board, platforms=set(), pending_only=True) == []
    assert len(notify.list_notify_subs(board)) == 2


@pytest.mark.parametrize('status', ['ready', 'review'])
def test_no_guard_candidates_avoids_registry_then_new_hold_runs(board, monkeypatch, status):
    calls = Mock(return_value={})
    monkeypatch.setattr(node, 'dispatch_refusals', calls)
    for _ in range(20):
        guards.sweep_remote_guard_holds(board, dispatch.DispatchResult())
    assert calls.call_count == 0
    task = kb.create_task(board, title='new hold', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task))
        kb._append_event(board, task, 'respawn_guarded', {'reason': 'blocker_auth'})
    guards.sweep_remote_guard_holds(board, dispatch.DispatchResult())
    assert calls.call_count == 2


def test_empty_reclaim_skips_worker_sweeps_but_running_retries(board, monkeypatch):
    callbacks = []
    for module, name, answer in (
        (kb, 'release_stale_claims', 0),
        (dispatch, 'reconcile_orphaned_running', []),
        (dispatch, 'detect_stale_running', []),
        (dispatch, 'detect_crashed_workers', []),
        (dispatch, 'enforce_max_runtime', []),
    ):
        callback = Mock(return_value=answer)
        if name == 'detect_crashed_workers':
            callback._last_auto_blocked = []
            callback._last_rate_limited = []
            callback._last_owner_restarted = []
        monkeypatch.setattr(module, name, callback)
        callbacks.append(callback)
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch._worker_scope, 'stopped_scope_units', lambda *a, **kw: [])

    def tick():
        dispatch._run_reclaim_phase(board, dispatch.DispatchResult(),
                                    stale_timeout_seconds=100, failure_limit=2, reconcile_orphans=True)
    for _ in range(3):
        tick()
    assert all(callback.call_count == 0 for callback in callbacks)
    task = kb.create_task(board, title='running worker', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task,))
    # No intervening board write: PID death and deadline expiry must still be checked.
    for _ in range(3):
        tick()
    assert all(callback.call_count == 3 for callback in callbacks)


def test_no_running_tasks_still_closes_dangling_runs(board, monkeypatch):
    task = kb.create_task(board, title='old ended task', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (task,))
        run = board.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)", (task,)).lastrowid
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch._worker_scope, 'stopped_scope_units', lambda *a, **kw: [])
    dispatch._run_reclaim_phase(board, dispatch.DispatchResult(), stale_timeout_seconds=100,
                                failure_limit=2, reconcile_orphans=True)
    assert board.execute('SELECT status FROM task_runs WHERE id=?', (run,)).fetchone()[0] == 'reclaimed'


def test_reader_pruning_leaves_active_borrower_usable(board, monkeypatch):
    from hermes_cli import kanban_reader_retention as retention
    from hermes_cli import kanban_session_activity as activity
    path = Path(board.execute('PRAGMA database_list').fetchone()[2]).resolve()
    monkeypatch.setattr(retention, 'LIMIT', 0)
    with retention.reader(path) as conn:
        retention.prune(missing=str(path))
        assert conn.execute('SELECT 1').fetchone()[0] == 1
        assert str(path) in activity._RO_CACHE
    assert str(path) not in activity._RO_CACHE


def test_empty_blocker_sweeps_skip_and_new_candidates_run(board, monkeypatch):
    from hermes_cli import kanban_shared_blockers as blockers
    release, probe = Mock(return_value=[]), Mock(return_value=[])
    monkeypatch.setattr(kb, 'recompute_ready', lambda *a, **kw: [])  # isolate candidate gates from block-policy repair
    monkeypatch.setattr(blockers, 'release_cleared_blockers', release)
    monkeypatch.setattr(blockers, 'probe_stranded_blockers', probe)
    monkeypatch.setattr(dispatch, 'reap_worker_zombies', lambda: None)
    monkeypatch.setattr(dispatch._worker_scope, 'stopped_scope_units', lambda *a, **kw: [])
    def tick():
        dispatch._run_reclaim_phase(board, dispatch.DispatchResult(),
                                    stale_timeout_seconds=100, failure_limit=2, reconcile_orphans=True)
    for _ in range(3):
        tick()
    assert release.call_count == probe.call_count == 0
    task = kb.create_task(board, title='blocker candidate', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task,))
    for _ in range(3):
        tick()
    # [daveey] G2: a candidate runs its lane, then the lane skips until the content revision moves or a due
    # time it returned arrives (``kanban_sweep_gate``); the mocks return no due time.
    assert release.call_count == probe.call_count == 1
    kb.add_comment(board, task, 'someone', 'a content write')
    tick()
    assert release.call_count == probe.call_count == 2


def test_owner_archive_candidate_scan_is_not_repeated(board, monkeypatch):
    from hermes_cli import kanban_owner_archive_relay as relay
    task = kb.create_task(board, title='answered archive', assignee='default')
    with kbc.write_txn(board):
        kb._append_event(board, task, relay.REQUEST_KIND, {'request_uid': 'handled'})
        kb._append_event(board, task, relay.RESULT_KIND, {'request_uid': 'handled'})
    monkeypatch.setattr(relay, '_home_of', lambda *a: None)
    queries = []
    board.set_trace_callback(queries.append)
    try:
        for _ in range(4):
            assert relay.apply_requests(board) == []
        assert sum(q.startswith('SELECT t.id FROM tasks t') for q in queries) <= 1
    finally:
        board.set_trace_callback(None)


@pytest.mark.parametrize('status', ['done', 'archived'])
@pytest.mark.parametrize('event_kind', ['cancel', 'forwarded'])
def test_remote_archive_empty_sources_skipped_but_pending_source_retried(board, monkeypatch, status, event_kind):
    from hermes_cli import kanban_owner_archive_relay as relay
    from hermes_cli import kanban_remote_stop as stop
    from hermes_cli import kanban_placement_forward as placement
    stop_call, owner_call, placement_call = (Mock(return_value=[]) for _ in range(3))
    monkeypatch.setattr(stop, 'apply_stop_requests', stop_call)
    monkeypatch.setattr(relay, 'apply_requests', owner_call)
    monkeypatch.setattr(placement, 'sweep_unresolved_archives', placement_call)
    for _ in range(3):
        assert relay.apply_remote_archives(board) == []
    assert (stop_call.call_count, owner_call.call_count, placement_call.call_count) == (0, 0, 0)
    task = kb.create_task(board, title='remote archive', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task))
        kind = placement.CANCEL_INTENT if event_kind == 'cancel' else placement.ARCHIVE_FORWARDED
        kb._append_event(board, task, kind, {})
    # A pending source can resolve in the remote replica without a local write.
    for _ in range(3):
        relay.apply_remote_archives(board)
    assert placement_call.call_count == 3
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task,))
        kb._append_event(board, task, stop.STOP_KIND, {})
        kb._append_event(board, task, relay.REQUEST_KIND, {})
    relay.apply_remote_archives(board)
    assert stop_call.call_count == owner_call.call_count == 1


def test_candidate_memo_skips_scan_and_external_status_commit_invalidates(board):
    from hermes_cli.kanban_idle_candidates import _on_connection as event_candidates
    task = kb.create_task(board, title='held history', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task,))
        kb._append_event(board, task, 'respawn_guarded', {})
    queries = []
    board.set_trace_callback(queries.append)
    try:
        for _ in range(20):
            assert not event_candidates(board, ('ready',), ('respawn_guarded',))
        scans = lambda: sum(q.startswith('SELECT t.id FROM tasks t') for q in queries)
        assert scans() == 1
        path = Path(board.execute('PRAGMA database_list').fetchone()[2])
        writer = kbc.connect(path)
        try:
            with kbc.write_txn(writer):
                writer.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task,))
        finally:
            writer.close()
        assert event_candidates(board, ('ready',), ('respawn_guarded',))
        assert scans() == 2
    finally:
        board.set_trace_callback(None)


def test_candidate_memo_survives_dispatch_connection_reopen(board, monkeypatch):
    from hermes_cli import kanban_idle_candidates as candidates
    from hermes_cli.kanban_reader_retention import reader
    path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    queries = []
    with reader(path) as observer:
        observer.set_trace_callback(queries.append)
    try:
        for _ in range(3):
            conn = kbc.connect(path)
            conn.set_trace_callback(queries.append)
            try:
                assert not candidates.event_candidates(conn, ('ready',), ('respawn_guarded',))
            finally:
                conn.close()
        assert sum(q.startswith('SELECT t.id FROM tasks t') for q in queries) == 1
    finally:
        with reader(path) as observer:
            observer.set_trace_callback(None)


def test_candidate_key_failure_falls_back_to_real_query(board, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_idle_candidates as candidates
    monkeypatch.setattr(candidates.revision, 'read_key', Mock(side_effect=sqlite3.OperationalError('key unavailable')))
    task = kb.create_task(board, title='candidate despite key failure', assignee='default')
    with kbc.write_txn(board):
        board.execute("UPDATE tasks SET status='ready' WHERE id=?", (task,))
        kb._append_event(board, task, 'respawn_guarded', {})
    assert candidates.event_candidates(board, ('ready',), ('respawn_guarded',))


def test_registry_concurrent_misses_are_single_flight(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Condition, Event, Lock
    condition, release = Condition(), Event()
    arrivals = {'lock': 0, 'fetch': 0}
    # Freeze before the first fetch: wall time and fractional TTL rounding must
    # not decide whether concurrent readers share the initial cache entry.
    clock = SimpleNamespace(monotonic=lambda: 1000.0)
    monkeypatch.setattr(node, 'time', clock)

    class ObservedLock:
        """Report attempts before blocking, without changing lock semantics."""
        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            with condition:
                arrivals['lock'] += 1
                condition.notify_all()
            self.lock.acquire()

        def __exit__(self, *exc):
            self.lock.release()

    def blocked_fetch():
        with condition:
            arrivals['fetch'] += 1
            condition.notify_all()
        assert release.wait(timeout=10)
        return {'me': 'local', 'machines': [{'name': 'one'}]}

    fetch = Mock(side_effect=blocked_fetch)
    node.set_registry_fetch(fetch)
    monkeypatch.setattr(node, '_REGISTRY_LOCK', ObservedLock())
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(node.registry) for _ in range(8)]
            try:
                with condition:
                    # Every caller has reached the lock, or an unprotected fetch.
                    assert condition.wait_for(
                        lambda: (arrivals['lock'] == 8 and arrivals['fetch'] >= 1)
                        or arrivals['fetch'] == 8, timeout=10)
                    assert arrivals['fetch'] >= 1
            finally:
                release.set()
            values = [future.result(timeout=10) for future in futures]
        assert all('one' in v['machines'] for v in values)
        assert fetch.call_count == 1
        clock.monotonic = lambda: 1000.0 + node.REGISTRY_TTL_S
        fetch.side_effect = None
        fetch.return_value = {'me': 'local', 'machines': [{'name': 'two'}]}
        assert 'two' in node.registry()['machines']
        assert fetch.call_count == 2
    finally:
        node.set_registry_fetch(None)


@pytest.mark.parametrize('public', [False, True])
def test_candidate_memo_bypassed_for_transactions_and_missing_triggers(board, public):
    from hermes_cli import kanban_activity_revision as revision
    from hermes_cli import kanban_idle_candidates as candidates
    event_candidates = candidates.event_candidates if public else candidates._on_connection
    assert not event_candidates(board, ('ready',), ('respawn_guarded',))
    board.execute('BEGIN')
    try:
        task = 'transaction-only-task'
        board.execute("INSERT INTO tasks(id,title,status,created_at) VALUES (?, 'temporary', 'ready', 1)", (task,))
        kb._append_event(board, task, 'respawn_guarded', {})
        assert event_candidates(board, ('ready',), ('respawn_guarded',))
    finally:
        board.rollback()
    assert not event_candidates(board, ('ready',), ('respawn_guarded',))
    revision.drop_triggers(board)
    # Trace the caller connection rather than the retained reader for this check.
    event_candidates = candidates._on_connection
    queries = []
    board.set_trace_callback(queries.append)
    try:
        for _ in range(2):
            assert not event_candidates(board, ('ready',), ('respawn_guarded',))
        assert sum(q.startswith('SELECT t.id FROM tasks t') for q in queries) == 2
    finally:
        board.set_trace_callback(None)
