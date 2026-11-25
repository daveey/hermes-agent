"""Exercise native scheduling through the actual annotation projection path."""
import contextvars
import errno
import os
import resource
import sqlite3

import pytest
import threading
import time
from types import SimpleNamespace

from hermes_cli import kanban_annotation_disk as disk
from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.annotation_event_loop import run
from hermes_cli.annotation_file_events import FileEvents
from tests.hermes_cli import test_kanban_annotation_host as helpers
from tests.hermes_cli.annotation_holder_fixture import PrivateHolderEvents


def _no_confirmations(monkeypatch, delays=()):
    """Isolate scheduling units from (or pin) the post-invalidation confirmations."""
    monkeypatch.setattr(host, 'SETTLE_S', delays)
    monkeypatch.setattr(host, '_unsettled', set())
    monkeypatch.setattr(host, '_settle_deadlines', {})


def test_source_invalidation_gets_bounded_coalesced_confirmations(monkeypatch, tmp_path):
    scope = ('confirm-test',)
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    _no_confirmations(monkeypatch, (0.25, 2.0))
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / 'copy')
    monkeypatch.setattr(disk, 'demand_age', lambda scope: 0)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    now = [100.0]
    monkeypatch.setattr(host.time, 'time', lambda: now[0])
    calls = []
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', calls.append)
    events = SimpleNamespace(replace=lambda *a, **kw: None, control_only=False)
    assert host._event_refresh(events) == 100.25  # a source read is not yet settled
    assert host._clock_deadlines[scope] == [100.25, 102.0]
    now[0] = 100.125
    assert host._event_refresh(events) == 100.375  # a newer invalidation replaces, never stacks
    assert host._clock_deadlines[scope] == [100.375, 102.125]
    events.control_only = True  # demand churn / timer expiry carry no source event
    assert host._event_refresh(events) == 100.375
    assert len(calls) == 2, 'demand consumed a confirmation'
    now[0] = 100.5
    assert host._event_refresh(events) == 102.125
    now[0] = 102.25
    assert host._event_refresh(events) is None, 'a confirmation scheduled another one'
    assert len(calls) == 4
    assert host._event_refresh(events) is None
    assert len(calls) == 4, 'settled source was read again while idle'


def test_failed_coverage_install_keeps_source_invalidation_pending(monkeypatch, tmp_path):
    scope = ('install-test',)
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    monkeypatch.setattr(host, '_coverage_stale', set(), raising=False)
    _no_confirmations(monkeypatch)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    # A far provider clock: the retry must not wait for it either.
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), 160.0))
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / 'copy')
    monkeypatch.setattr(disk, 'demand_age', lambda scope: 0)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(host.time, 'time', lambda: 100.0)
    calls = []
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', calls.append)
    broken = [False]

    def replace(*args, **kwargs):
        if broken[0]:
            raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
    events = SimpleNamespace(replace=replace, control_only=False)
    assert host._event_refresh(events) == 160.0
    assert calls == [scope]
    broken[0] = True  # a source event arrives while inotify instances are exhausted
    with pytest.raises(OSError):
        host._event_refresh(events)
    assert calls == [scope], 'read without installed coverage'
    events.control_only = True  # the loop's retry is a wait timeout; the event is already drained
    broken[0] = False
    assert host._event_refresh(events) == 160.0
    assert calls == [scope, scope], 'failed install dropped the source invalidation'
    assert host._event_refresh(events) == 160.0
    assert calls == [scope, scope], 'recovered coverage was read again while idle'


@pytest.mark.parametrize('wal', [False, True])
def test_real_projection_survives_failed_coverage_install(tmp_path, monkeypatch, wal):
    """Real loop, FileEvents and projection; only replace() fails for one second."""
    scope = ('install-loop', 'default')
    path = tmp_path / 'kanban.db'
    conn = helpers._board(path, [('t1', 'running', 's1')])
    if wal:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        conn.execute('SELECT * FROM tasks').fetchall()
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: None)
    monkeypatch.setattr(ksa, '_replica_paths', list)
    monkeypatch.setattr(ksa, '_remote_cards', lambda *a, **kw: [])
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(ksa, '_done_checks', {})
    monkeypatch.setattr(host, 'core_sha', lambda: 'a' * 40)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(path)], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    disk.touch_demand(scope)
    ready, stop, done = threading.Event(), threading.Event(), threading.Event()
    calls = []
    refresh = ksa._refresh_annotation_locked

    def record(scope):
        result = refresh(scope)
        calls.append(dict(result[0]))
        ready.set()
        if not result[0]:
            done.set()
        return result
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', record)
    with FileEvents() as events:
        original = events.replace
        armed, failed = [], []

        def replace(*args, **kwargs):
            if armed and (not failed or time.monotonic() < failed[0] + 1.0):
                failed.append(time.monotonic())
                raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))
            return original(*args, **kwargs)
        monkeypatch.setattr(events, 'replace', replace)
        thread = threading.Thread(target=run, args=(
            events, stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
            lambda: host._event_refresh(events)))
        thread.start()
        try:
            assert ready.wait(5)
            stop.wait(max(host.SETTLE_S) + .5)  # ownership confirmations spent
            armed.append(True)
            disk.touch_demand(scope)
            conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
            delivered = done.wait(8)
            assert failed, 'the install failure was never injected'
            assert delivered, f'replace failed {len(failed)}x; commit not projected; reads={calls}'
        finally:
            stop.set()
            events.wake()
            thread.join(5)
            conn.close()
            ksa._reset_cache_for_tests()


def test_failed_scope_does_not_starve_other_scopes(monkeypatch, tmp_path):
    current = contextvars.ContextVar('annotation-test-scope')
    scopes = [('failed',), ('healthy',)]
    contexts = {}
    for scope in scopes:
        ctx = contextvars.copy_context()
        ctx.run(current.set, scope)
        contexts[scope] = ctx
    monkeypatch.setattr(host, '_known', contexts)
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    _no_confirmations(monkeypatch)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / scope[0])
    monkeypatch.setattr(disk, 'demand_age', lambda scope: 0)
    monkeypatch.setattr(ksa, '_origin_scope', current.get)
    calls = []
    def refresh(scope):
        calls.append(scope)
        if scope == scopes[0]:
            raise OSError('failed source')
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    events = SimpleNamespace(replace=lambda *a, **kw: None)
    started = time.time()
    due = host._event_refresh(events)
    assert calls == scopes
    assert due is not None
    assert started < due <= time.time() + host.HOLDER_INTERVAL_S
    assert host._event_ready == {scopes[1]}


def test_dormant_scope_does_not_amplify_active_demand(monkeypatch, tmp_path):
    current = contextvars.ContextVar('dormant-scope')
    active, dormant = ('active',), ('dormant',)
    contexts = {}
    for scope in (active, dormant):
        ctx = contextvars.copy_context()
        ctx.run(current.set, scope)
        contexts[scope] = ctx
    monkeypatch.setattr(host, '_known', contexts)
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    _no_confirmations(monkeypatch)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / scope[0])
    ages = {active: 0, dormant: None}
    monkeypatch.setattr(disk, 'demand_age', ages.get)
    monkeypatch.setattr(ksa, '_origin_scope', current.get)
    calls, replacements = [], []
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', calls.append)
    events = SimpleNamespace(replace=lambda *a, **kw: replacements.append(kw), control_only=False)
    host._event_refresh(events)
    assert calls == [active]
    events.control_only = True
    for _ in range(10):
        host._event_refresh(events)
    assert calls == [active], 'dormant scope caused unchanged active inputs to be reread'
    assert len(replacements) == 1, 'demand discarded queued native source events'
    assert dormant not in host._event_ready, 'dormant is not a certified projection'
    ages[dormant] = 0
    host._event_refresh(events)
    assert dormant in calls, 'first demand must activate the dormant scope'
    assert dormant in host._event_ready


def test_coverage_failure_does_not_starve_healthy_scope(monkeypatch, tmp_path):
    current = contextvars.ContextVar('coverage-scope')
    failed, healthy = ('failed-coverage',), ('healthy-coverage',)
    contexts = {}
    for scope in (failed, healthy):
        ctx = contextvars.copy_context()
        ctx.run(current.set, scope)
        contexts[scope] = ctx
    monkeypatch.setattr(host, '_known', contexts)
    monkeypatch.setattr(host, '_clock_deadlines', {})
    monkeypatch.setattr(host, '_event_ready', set())
    _no_confirmations(monkeypatch)
    broken = [True]
    def paths(scope):
        if scope == failed and broken[0]:
            raise OSError('source discovery unavailable')
        return [], []
    monkeypatch.setattr(host, '_watch_paths', paths)
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / scope[0])
    monkeypatch.setattr(disk, 'demand_age', lambda scope: 0)
    monkeypatch.setattr(ksa, '_origin_scope', current.get)
    monkeypatch.setattr(host.time, 'time', lambda: 100.0)
    calls = []
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', calls.append)
    events = SimpleNamespace(replace=lambda *a, **kw: None, control_only=False)
    assert host._event_refresh(events) == 102.0
    assert calls == [healthy]
    assert host._event_ready == {healthy}
    events.control_only = True
    assert host._event_refresh(events) == 102.0
    assert calls == [healthy], 'demand bypassed coverage retry backoff'
    broken[0] = False
    monkeypatch.setattr(host.time, 'time', lambda: 103.0)
    host._event_refresh(events)
    assert failed in calls, 'coverage error never recovered without source write'
    assert host._event_ready == {failed, healthy}


@pytest.mark.parametrize('outcome', ['cached-clock', 'inactive', 'error', 'busy'])
def test_scope_deadlines_preserve_cached_clocks_and_retry(monkeypatch, tmp_path, outcome):
    scope = ('clock-test',)
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    monkeypatch.setattr(host, '_clock_deadlines', {scope: [10000.0]})
    monkeypatch.setattr(host, '_event_ready', {scope})
    _no_confirmations(monkeypatch)
    monkeypatch.setattr(host.time, 'time', lambda: 100.0)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    monkeypatch.setattr(host, 'waiting_events', lambda now: (set(), None))
    monkeypatch.setattr(disk, '_path', lambda scope: tmp_path / 'copy')
    monkeypatch.setattr(disk, 'demand_age', lambda scope: None if outcome == 'inactive' else 0)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    calls = []
    def refresh(scope):
        calls.append(scope)
        if outcome == 'error':
            raise OSError('source unavailable')
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    if outcome == 'busy':
        monkeypatch.setattr(ksa, '_annotate_refresh_lock', SimpleNamespace(acquire=lambda **kw: False))
    events = SimpleNamespace(replace=lambda *a, **kw: None, control_only=False)
    due = host._event_refresh(events)
    expected = [] if outcome == 'inactive' else [10000.0]
    if outcome in ('error', 'busy'):
        expected.insert(0, 102.0)
    assert host._clock_deadlines.get(scope, []) == expected
    assert due == (expected[0] if expected else None)
    if outcome in ('error', 'busy'):
        events.control_only = True
        assert host._event_refresh(events) == 102.0
        assert len(calls) == (1 if outcome == 'error' else 0), 'control wake bypassed retry backoff'
    if outcome == 'cached-clock':
        events.control_only = True
        assert host._event_refresh(events) == 10000.0
        assert calls == [scope]


@pytest.mark.parametrize('complete,fresh', [(False, True), (True, False)])
def test_degraded_board_read_schedules_recovery(monkeypatch, tmp_path, complete, fresh):
    path = tmp_path / 'kanban.db'
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(ksa, '_board_memo', {})
    monkeypatch.setattr(ksa._revalidate, 'board_memo_key', lambda path: 'key')
    reads = {'active': {}, 'blocked': {}, 'unseen': {}, 'pairs': set(),
             'parked': ({}, {}), 'stall_rows': (set(), {}, {})}
    monkeypatch.setattr(ksa._busy, 'board_reads', lambda *args: (reads, complete, fresh))
    deadlines = []
    monkeypatch.setattr(host, 'note_deadline', deadlines.append)
    started = time.time()
    ksa._read_local_boards({}, {}, {})
    assert deadlines, 'degraded last-good display must retry without another source event'
    assert started < min(deadlines) <= time.time() + host.HOLDER_INTERVAL_S


def test_board_batch_closes_connection_before_return(tmp_path, monkeypatch):
    path = tmp_path / 'kanban.db'
    writer = helpers._board(path, [('t1', 'running', 's1')])
    opened = []
    original = ksa._open_ro
    def open_one(path):
        conn = original(path)
        opened.append(conn)
        return conn
    monkeypatch.setattr(ksa, '_open_ro', open_one)
    try:
        reads, complete = ksa._read_board(path, parked=True, stalled=True)
        assert reads['active'] == {'s1': 1}
        assert complete
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
    finally:
        writer.close()


def test_board_execution_derives_after_connection_close(tmp_path, monkeypatch):
    from hermes_cli import kanban_execution_source as source

    path = tmp_path / 'kanban.db'
    writer = helpers._board(path, [('t1', 'running', 's1')])
    opened, derived = [], []
    original_open, original_raw = ksa._open_ro, source.safe_raw

    def open_one(path):
        conn = original_open(path)
        opened.append(conn)
        return conn

    def derive(task, inputs):
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
        derived.append(task['id'])
        return original_raw(task, inputs)

    monkeypatch.setattr(ksa, '_open_ro', open_one)
    monkeypatch.setattr(source, 'safe_raw', derive)
    try:
        reads, complete = ksa._read_board(path, parked=True, stalled=True)
        assert complete and reads['active'] == {'s1': 1}
        assert derived == ['t1']
    finally:
        writer.close()


@pytest.mark.parametrize('action', ['remove', 'read-error', 'deadline', 'links-clock', 'attention-window',
                                   'done-source', 'done-clock', 'done-error', 'done-register', 'watch-loss', 'overflow'])
def test_real_projection_removal_and_read_recovery(tmp_path, monkeypatch, action):
    scope = ('native-recovery-test', 'default')
    path = tmp_path / 'kanban.db'
    writer = helpers._board(path, [('t1', 'running', 's1')])
    # Give semantic lineage probing a real isolated store. Missing state is
    # deliberately fail-fresh and would turn settle confirmations into forced reads.
    from hermes_state import SessionDB
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    state = SessionDB(db_path=tmp_path / 'state.db')
    state.create_session('s1', 'cli')
    state.close()
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [path] if path.exists() else [])
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: None)
    monkeypatch.setattr(ksa, '_replica_paths', list)
    monkeypatch.setattr(ksa, '_remote_cards', lambda *a, **kw: [])
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(ksa, '_done_checks', {})
    monkeypatch.setattr(host, 'core_sha', lambda: 'a' * 40)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(path)], []))
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    disk.touch_demand(scope)
    ready, changed, failed, stop, reread = (threading.Event() for _ in range(5))
    fail_next = []
    original_parts = ksa._read_annotation_parts
    original_refresh = ksa._refresh_annotation_locked
    outputs = []
    boundary = []
    if action.startswith('done-'):
        monkeypatch.setattr(host, '_done_event_sources', {})
        check_path = tmp_path / 'external-check.txt'
        check_path.write_text('no')
        attempts = []
        monkeypatch.setattr(ksa._recent_done, 'memoized', lambda *args:
            [('s1', {'id': 'done-card', 'completed_at': time.time() - 10})])
        def check(card):
            attempts.append(True)
            if action == 'done-error' and len(attempts) == 1:
                raise OSError('transient done input failure')
            if action == 'done-clock':
                if not boundary:
                    boundary.append(time.time() + .5)
                return time.time() >= boundary[0]
            return action in ('done-error', 'done-register') or check_path.read_text() == 'yes'
        def register_check():
            return ksa.register_done_check('external-check', check,
                watch_paths=lambda: [check_path],
                next_deadline=lambda: boundary[0] if boundary and time.time() < boundary[0] else None)
        if action != 'done-register':
            register_check()
    if action == 'attention-window':
        monkeypatch.setattr(ksa, '_done_checks', {'clock-check': lambda card: True})
        def recent(path, since, *args):
            if not boundary:
                boundary.append(time.time() + .5)
            completed = boundary[0] - ksa.ATTENTION_WINDOW_S
            return [('s1', {'id': 'done-card', 'completed_at': completed})] if completed >= since else []
        monkeypatch.setattr(ksa._recent_done, 'memoized', recent)
    if action == 'links-clock':
        monkeypatch.setattr(host, '_event_sources', {})
        monkeypatch.setattr(ksa, '_links_fingerprint', lambda: ('stable-source',))
        def project_links():
            if not boundary:
                boundary.append(time.time() + .5)
            host.note_deadline(boundary[0])
            return [{'key': 'clock-card'}] if time.time() < boundary[0] else []
        monkeypatch.setattr(ksa, '_project_remote_links', project_links)
        def waiting_links():
            cards = ksa._remote_links()
            return {'s1': [card['key'] for card in cards]} if cards else {}
        ksa.register_waiting_source('links-clock', waiting_links,
            watch_paths=list, next_deadline=lambda: None)
    if action == 'deadline':
        monkeypatch.setattr(host, '_event_sources', {})
        def waiting():
            if not boundary:
                boundary.append(time.time() + .5)
            return {'s1': ['clock-card']} if time.time() < boundary[0] else {}
        ksa.register_waiting_source('clock-provider', waiting, watch_paths=list,
                                   next_deadline=lambda: boundary[0] if boundary and time.time() < boundary[0] else None)

    def read_parts():
        if fail_next:
            fail_next.pop()
            failed.set()
            raise sqlite3.OperationalError('injected source read failure')
        return original_parts()

    def refresh(scope):
        result = original_refresh(scope)
        outputs.append(result)
        ready.set()
        reread.set()
        # Confirmation re-reads of an unchanged source are not transitions.
        if len(outputs) > 1 and outputs[-1] != outputs[-2]:
            changed.set()
        return result

    monkeypatch.setattr(ksa, '_read_annotation_parts', read_parts)
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', refresh)
    with FileEvents() as events:
        monkeypatch.setattr(host, '_native_events', [events])
        thread = threading.Thread(target=run, args=(events, stop,
            SimpleNamespace(held=lambda: True, beat=lambda: None), lambda: host._event_refresh(events)))
        thread.start()
        try:
            assert ready.wait(5)
            assert outputs[-1][0] == {'s1': 1}
            if action.startswith('done-'):
                assert outputs[-1][10] == {}
                if action == 'done-source':
                    check_path.write_text('yes')
                elif action == 'done-register':
                    dispose = register_check()
                budget = host.HOLDER_INTERVAL_S + 1 if action == 'done-error' else 1
                assert changed.wait(budget), 'done input transition did not update native projection'
                assert outputs[-1][10] == {'s1': 1}
                if action == 'done-register':
                    changed.clear()
                    dispose()
                    assert changed.wait(1), 'unregister did not update native projection'
                    assert outputs[-1][10] == {}
            elif action == 'attention-window':
                assert outputs[-1][10] == {'s1': 1}
                assert changed.wait(1), 'attention window needed a source write'
                assert outputs[-1][10] == {}
                assert outputs[-1][0] == {'s1': 1}
            elif action in ('deadline', 'links-clock'):
                assert outputs[-1][9] == {'s1': frozenset({'clock-card'})}
                assert changed.wait(1), 'declared clock needed a source write'
                assert outputs[-1][9] == {}
                assert outputs[-1][0] == {'s1': 1}
            elif action in ('watch-loss', 'overflow'):
                # Let the ownership read's confirmations finish: the re-read below
                # must come from the loss itself, with unchanged output.
                stop.wait(max(host.SETTLE_S) + .5)
                reread.clear()
                if action == 'watch-loss':
                    # Real kernel IN_IGNORED, not a mocked refresh or wake result.
                    assert events._lib.inotify_rm_watch(events._fd, next(iter(events._names))) == 0
                else:
                    # Inject the kernel's overflow packet at the read boundary;
                    # retain the real wait, decoding, scheduling and projection.
                    from hermes_cli import annotation_file_events as native
                    original_read = native.os.read
                    injected = []
                    def overflow_read(fd, length):
                        data = original_read(fd, length)
                        if fd == events._fd and not injected:
                            injected.append(True)
                            return native._HEADER.pack(-1, native._OVERFLOW, 0, 0)
                        return data
                    monkeypatch.setattr(native.os, 'read', overflow_read)
                    disk.touch_demand(scope)
                assert reread.wait(1), 'notification loss did not invalidate the real projection'
                assert outputs[-1][0] == {'s1': 1}
                changed.clear()
                writer.execute("UPDATE tasks SET status='done' WHERE id='t1'")
                assert changed.wait(1), 'coverage was not reinstalled after notification loss'
                assert outputs[-1][0] == {}
            elif action == 'remove':
                writer.close()
                path.unlink()
                assert changed.wait(1), 'removed source did not clear projection'
            else:
                fail_next.append(True)
                writer.execute("UPDATE tasks SET status='done' WHERE id='t1'")
                assert failed.wait(1)
                assert changed.wait(host.HOLDER_INTERVAL_S + 1), 'read failure needed another source write'
            if not action.startswith('done-') and action not in ('deadline', 'links-clock', 'attention-window'):
                assert outputs[-1][0] == {}
        finally:
            stop.set()
            events.wake()
            thread.join(5)
            writer.close()
            ksa._reset_cache_for_tests()
    assert not thread.is_alive()


def test_real_projection_idle_and_changed_source(tmp_path, monkeypatch):
    scope = ('native-holder-test', 'default')
    path = tmp_path / 'kanban.db'
    conn = helpers._board(path, [('t1', 'running', 's1')])
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: None)
    monkeypatch.setattr(ksa, '_replica_paths', list)
    monkeypatch.setattr(ksa, '_remote_cards', lambda *a, **kw: [])
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(ksa, '_done_checks', {})
    monkeypatch.setattr(host, 'core_sha', lambda: 'a' * 40)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(path)], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(host, '_known', {
        scope: contextvars.copy_context(),
        ('native-dormant-test', 'default'): contextvars.copy_context(),
    })
    disk.touch_demand(scope)
    ready, updated, stop = threading.Event(), threading.Event(), threading.Event()
    calls = []
    refresh = ksa._refresh_annotation_locked
    def record(scope):
        result = refresh(scope)
        calls.append(result)
        ready.set()
        if len(calls) > 1:
            updated.set()
        return result
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', record)
    with PrivateHolderEvents() as events:
        usage = []
        def worker():
            before = resource.getrusage(resource.RUSAGE_THREAD)
            started = time.monotonic()
            run(events, stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
                lambda: host._event_refresh(events))
            after = resource.getrusage(resource.RUSAGE_THREAD)
            usage.append((after.ru_nvcsw - before.ru_nvcsw) / (time.monotonic() - started))
        thread = threading.Thread(target=worker, name='kanban-annotation-holder')
        thread.start()
        try:
            assert ready.wait(5)
            assert calls[0][0] == {'s1': 1}
            # The initial ownership read gets its bounded visibility confirmations;
            # after them nothing may read while idle, whatever the demand churn.
            stop.wait(max(host.SETTLE_S) + .5)
            assert len(calls) <= 1 + len(host.SETTLE_S)
            updated.clear()
            os.utime(tmp_path)  # owned reproduction of an unrelated ancestor IN_ATTRIB
            idle_until = time.monotonic() + 61
            while time.monotonic() < idle_until:
                disk.touch_demand(scope)
                disk.touch_demand(scope, strict=True)
                assert not updated.wait(.1), 'demand-only notification reread source inputs'
            # Renew demand before mutation; demand is intentionally not one of the
            # overridden source paths, but actual host coverage still watches it.
            disk.touch_demand(scope)
            conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
            assert updated.wait(1)
            assert calls[-1][0] == {}
        finally:
            stop.set()
            events.wake()
            thread.join(5)
            conn.close()
    assert not thread.is_alive()
    assert usage[0] < 500
    print(f'actual projection fixture voluntary cs/s with demand churn: {usage[0]:.3f}')
    ksa._reset_cache_for_tests()


def test_wal_commit_racing_its_notification_is_confirmed(tmp_path, monkeypatch):
    """A -wal write notifies before the mmap'd index publishes the commit.

    Real WAL writer, real projection, no wake/request/unrelated event: every
    committed transition must reach the projection, and the source must go
    unread again once the bounded confirmations are spent.
    """
    scope = ('native-wal-test', 'default')
    path = tmp_path / 'kanban.db'
    conn = helpers._board(path, [('t1', 'running', 's1')])
    assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    conn.execute('SELECT * FROM tasks').fetchall()  # sidecars exist before the watches
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: None)
    monkeypatch.setattr(ksa, '_replica_paths', list)
    monkeypatch.setattr(ksa, '_remote_cards', lambda *a, **kw: [])
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(ksa, '_done_checks', {})
    monkeypatch.setattr(host, 'core_sha', lambda: 'a' * 40)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(path)], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    disk.touch_demand(scope)
    ready, stop = threading.Event(), threading.Event()
    seen = {True: threading.Event(), False: threading.Event()}
    calls = []
    delays = []
    refresh = ksa._refresh_annotation_locked

    def record(scope):
        result = refresh(scope)
        calls.append(dict(result[0]))
        ready.set()
        seen[bool(result[0])].set()
        return result
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', record)
    settle = max(host.SETTLE_S) + .5
    with PrivateHolderEvents() as events:
        thread = threading.Thread(target=run, args=(
            events, stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
            lambda: host._event_refresh(events)))
        thread.start()
        try:
            assert ready.wait(5)
            for index in range(24):
                running = index % 2 == 1
                seen[running].clear()
                disk.touch_demand(scope)
                began = time.monotonic()
                conn.execute('UPDATE tasks SET status=? WHERE id=?', ('running' if running else 'done', 't1'))
                assert seen[running].wait(settle), f'commit {index} never projected: {calls[-4:]}'
                delays.append(time.monotonic() - began)
            print(f'WAL commit→projection: max {max(delays):.3f}s, '
                  f'{sum(d > .2 for d in delays)} of {len(delays)} needed a confirmation read')
            stop.wait(settle)
            quiet = len(calls)
            os.utime(tmp_path)  # ancestor metadata is not a new WAL commit
            stop.wait(3)
            assert len(calls) == quiet, f'WAL source did not settle: {quiet} -> {len(calls)}'
        finally:
            stop.set()
            events.wake()
            thread.join(5)
            conn.close()
            ksa._reset_cache_for_tests()
    assert not thread.is_alive()


_READER = (
    'import sqlite3, sys\n'
    'for _ in range(12):\n'
    '    conn = sqlite3.connect(sys.argv[1], uri=True)\n'
    '    assert conn.execute("SELECT status FROM tasks").fetchall()\n'
    '    conn.close()\n'
)


@pytest.mark.parametrize('wal,mode', [(True, '?mode=ro'), (True, ''), (False, '')])
def test_foreign_reader_sessions_do_not_project(tmp_path, monkeypatch, wal, mode):
    """Another process that opens, selects and closes commits nothing.

    SQLite opens -wal/-shm (and, by default, the main file) read-write even to
    read, so every reader close raises IN_CLOSE_WRITE on a watched name. Real
    loop, real FileEvents, real projection: no read may follow, and a later
    commit must still be projected.
    """
    import subprocess
    import sys

    scope = ('native-reader-test', 'default')
    path = tmp_path / 'kanban.db'
    conn = helpers._board(path, [('t1', 'running', 's1')])
    if wal:
        assert conn.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
    conn.execute('SELECT * FROM tasks').fetchall()
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(ksa, '_hub_db_path', lambda: None)
    monkeypatch.setattr(ksa, '_replica_paths', list)
    monkeypatch.setattr(ksa, '_remote_cards', lambda *a, **kw: [])
    monkeypatch.setattr(ksa, '_waiting_sources', {})
    monkeypatch.setattr(ksa, '_done_checks', {})
    monkeypatch.setattr(host, 'core_sha', lambda: 'a' * 40)
    monkeypatch.setattr(host, '_watch_paths', lambda scope: ([str(path)], []))
    monkeypatch.setattr(host, '_discovery_paths', lambda: (set(), set()))
    ksa._reset_cache_for_tests()
    monkeypatch.setattr(host, '_known', {scope: contextvars.copy_context()})
    disk.touch_demand(scope)
    ready, done, stop = threading.Event(), threading.Event(), threading.Event()
    calls = []
    refresh = ksa._refresh_annotation_locked

    def record(scope):
        result = refresh(scope)
        calls.append(dict(result[0]))
        ready.set()
        if not result[0]:
            done.set()
        return result
    monkeypatch.setattr(ksa, '_refresh_annotation_locked', record)
    settle = max(host.SETTLE_S) + .5
    with PrivateHolderEvents() as events:
        thread = threading.Thread(target=run, args=(
            events, stop, SimpleNamespace(held=lambda: True, beat=lambda: None),
            lambda: host._event_refresh(events)))
        thread.start()
        try:
            assert ready.wait(5)
            stop.wait(settle)
            quiet = len(calls)
            os.utime(tmp_path)  # ancestor metadata must not be blamed on the readers
            subprocess.run([sys.executable, '-c', _READER, f'file:{path}{mode}'], check=True, timeout=30)
            stop.wait(settle)
            assert len(calls) == quiet, f'{len(calls) - quiet} projections followed 12 reader sessions'
            disk.touch_demand(scope)
            conn.execute("UPDATE tasks SET status='done' WHERE id='t1'")
            assert done.wait(settle), 'commit after reader sessions was not projected'
        finally:
            stop.set()
            events.wake()
            thread.join(5)
            conn.close()
            ksa._reset_cache_for_tests()
    assert not thread.is_alive()
