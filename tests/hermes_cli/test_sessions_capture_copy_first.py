"""One-shot capture can reuse recent publications but cannot publish incomplete reads."""
from contextlib import nullcontext
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_annotation_refresh as bounded


def role():
    # Exercise the old snapshot path too, rather than failing on a missing API.
    return getattr(bounded, 'capture_role', nullcontext)()


@pytest.fixture
def snapshot(monkeypatch):
    ksa._reset_cache_for_tests()
    scope = ('copy-first-test',)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: scope)
    monkeypatch.setattr(ksa, 'time', SimpleNamespace(time=lambda: 1000., monotonic=lambda: 10.))
    monkeypatch.setattr(bounded, 'time', ksa.time)
    monkeypatch.setattr(ksa._host_copy, 'demand', lambda *a, **kw: None)
    monkeypatch.setattr(ksa._host_copy, 'adopt_display', lambda *a: None)
    monkeypatch.setattr(ksa._host_copy, 'adopt', lambda *a: None)
    monkeypatch.setattr(ksa._host_copy, 'peek_display', lambda scope, parts: parts)
    monkeypatch.setattr(ksa._revalidate, 'refresh_if_unchanged', lambda *a: None)
    monkeypatch.setattr(ksa._external_activity, 'stamp_boards', lambda *a: None)
    monkeypatch.setattr(ksa._external_activity, 'stamp_replica_counts', lambda rows, *a: rows)
    parts = ksa._identity.freeze_annotation(({'target': 3}, *({} for _ in range(10))))
    monkeypatch.setattr(ksa._annotation_copy, 'load', lambda scope: (parts, 990.))
    calls, threads = [], []
    real = bounded.refresh_bounded
    def refresh(refresh, lock, previous, wait_s):
        calls.append(wait_s)
        def read():
            threads.append(threading.current_thread().name)
            return previous
        return real(read, lock, previous, wait_s)
    monkeypatch.setattr(bounded, 'refresh_bounded', refresh)
    yield scope, parts, calls, threads
    assert ksa._annotate_refresh_lock.acquire(timeout=5)
    ksa._annotate_refresh_lock.release()
    ksa._reset_cache_for_tests()


def test_fresh_restart_copy_no_thread_keeps_date_and_previous(snapshot):
    scope, _parts, calls, threads = snapshot
    with role():
        with ksa.one_annotation_snapshot():
            row = ksa.annotate_kanban_activity([{'id': 'target'}], display=True)[0]
        assert row['kanban_active'] == 3
        assert row['kanban_observed_at'] == 990.
        assert ksa._annotate_refresh_lock.acquire(blocking=False)
        ksa._annotate_refresh_lock.release()
        assert ksa._annotation_snapshot_owned(display=True) is ksa._last_annotation[scope]
    assert calls == []
    assert threads == []


@pytest.mark.parametrize('date', [969., 1001., None])
def test_unusable_copy_uses_bounded_refresh(snapshot, monkeypatch, date):
    _, parts, calls, threads = snapshot
    monkeypatch.setattr(ksa._annotation_copy, 'load', lambda scope: None if date is None else (parts, date))
    with role():
        ksa._annotation_snapshot_owned(display=True)
    assert calls == [1.]
    assert threads == ['kanban-annotation-refresh']


@pytest.mark.parametrize('active,display', [(True, False), (False, True)])
def test_strict_and_ordinary_callers_still_refresh(snapshot, active, display):
    with role() if active else nullcontext():
        ksa._annotation_snapshot_owned(display=display)
    assert snapshot[2] == [1.]
    assert snapshot[3] == ['kanban-annotation-refresh']


def test_candidate_exception_releases_lock(snapshot, monkeypatch):
    def fail(*args):
        raise ValueError('candidate date')
    monkeypatch.setattr(ksa._annotation_copy, 'observed_at', fail)
    with role(), pytest.raises(ValueError, match='candidate date'):
        ksa._annotation_snapshot_owned(display=True)
    assert not ksa._annotate_refresh_lock.locked()


@pytest.mark.parametrize('date,expected', [(1000., False), (970., False), (None, True), (float('nan'), True)])
def test_previous_read_date_fallback(snapshot, date, expected):
    scope, parts, calls, _ = snapshot
    ksa._last_annotation[scope] = parts
    if date is not None:
        bounded.record(scope, parts, date)
    with role():
        assert ksa._annotation_snapshot_owned(display=True) is parts
    assert bool(calls) is expected


@pytest.mark.parametrize('active', [True, False])
def test_helper_refresh_only_publishes_outside_capture(monkeypatch, tmp_path, active):
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_disk as disk
    ksa._reset_cache_for_tests()
    scope, full = ('publication-test',), ('inputs',)
    path = tmp_path / 'restart.json'
    monkeypatch.setattr(disk, 'FORCE', True)
    monkeypatch.setattr(disk, '_path', lambda scope: path)
    monkeypatch.setattr(host, 'prepare', lambda scope: (full, 1000., ('digest', True), None, None, None, None))
    monkeypatch.setattr(ksa, '_cache', {})
    monkeypatch.setattr(ksa, '_cache_scope', scope)
    monkeypatch.setattr(ksa, '_cache_fingerprint', full)
    monkeypatch.setattr(ksa, '_cache_revalidatable', True)
    parts = tuple({} for _ in range(11))
    roles = []
    def read():
        roles.append((bounded.in_capture_role(), threading.current_thread().name))
        return parts
    monkeypatch.setattr(ksa, '_read_annotation_parts', read)
    lock = threading.Lock()
    lock.acquire()
    try:
        with role() if active else nullcontext():
            assert bounded.refresh_bounded(lambda: host.refresh_locked(scope), lock, None, 5) is parts
        assert roles == [(active, 'kanban-annotation-refresh')]
        assert ksa._last_annotation[scope] is parts
        assert ksa._annotate_flight_result is parts
        assert bounded.read_at(parts) is not None
        assert path.exists() is (not active)
        assert path.with_suffix('.host.json').exists() is (not active)
    finally:
        assert lock.acquire(timeout=5)
        lock.release()
        ksa._reset_cache_for_tests()


@pytest.mark.parametrize('rollback,raises', [(False, False), (False, True), (True, False), (True, True)])
def test_worker_role_restored(monkeypatch, rollback, raises):
    import sys
    from hermes_cli import sessions_capture_worker as worker
    from hermes_cli import web_routers
    seen = []
    def reader(**selector):
        seen.append(bounded.in_capture_role())
        if raises:
            raise ValueError('reader failed')
        return {'sessions': []}
    reader.__wrapped__ = reader
    profiles = SimpleNamespace(get_profiles_sessions=reader, get_profiles_sessions_sidebar=reader,
                               get_profiles_session_stamps=reader)
    monkeypatch.setitem(sys.modules, 'hermes_cli.web_routers.profiles', profiles)
    monkeypatch.setattr(web_routers, 'profiles', profiles, raising=False)
    if rollback:
        monkeypatch.setenv('HERMES_SESSIONS_CAPTURE_COPY_FIRST', '0')
    else:
        monkeypatch.delenv('HERMES_SESSIONS_CAPTURE_COPY_FIRST', raising=False)
    assert not bounded.in_capture_role()
    with pytest.raises(ValueError, match='reader failed') if raises else nullcontext():
        assert worker.capture('sessions', {}) == b'{"sessions":[]}'
    assert seen == [not rollback]
    assert not bounded.in_capture_role()


@pytest.mark.parametrize('loaded,fails', [(True, False), (True, True), (False, False)])
def test_parent_demands_once_per_batch_without_boot_import(monkeypatch, loaded, fails):
    import builtins
    import sys
    from hermes_cli import sessions_native_source as native
    from hermes_cli.fact_capture import CaptureBudget
    from hermes_cli.fact_contract import Scope, SourceId
    from hermes_cli.fact_store import FactStore
    calls = []
    def demand(scope):
        calls.append(scope)
        if fails:
            raise RuntimeError('holder unavailable')
    monkeypatch.setattr(ksa._host_copy, 'demand', demand)
    monkeypatch.setattr(ksa, '_origin_scope', lambda: ('launch-profile',))
    if not loaded:
        monkeypatch.delitem(sys.modules, 'hermes_cli.kanban_session_activity')
    original_import = builtins.__import__
    def importing(name, globals=None, locals=None, fromlist=(), level=0):
        if not loaded and (name == 'hermes_cli.kanban_session_activity' or 'kanban_session_activity' in (fromlist or ())):
            pytest.fail('capture imported annotation at boot')
        return original_import(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, '__import__', importing)
    owner = native.SessionsSource(FactStore(), SourceId(Scope('install', 'scope'), 'sessions', 'test'),
                                 [], lambda *a: b'{}', capture_many=lambda views: [b'{}'] * len(views))
    owner._watch = SimpleNamespace(check=lambda: None)
    for batch in range(2):
        owner._next_capture = 0
        owner.demand('sessions', {'limit': batch})
        owner.demand('sessions_sidebar', {'limit': batch})
        capture = owner.capture(None, CaptureBudget(max_seconds=5))
        assert len(capture.facts) == 2
        assert all(f.error_code is None for f in capture.facts)
        owner.commit(capture)
    assert calls == [('launch-profile',)] * 2 if loaded else calls == []
