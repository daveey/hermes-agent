"""Unrelated generic reader retention cannot cool serialized waiting evidence."""
import contextlib
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_generation as generation
from hermes_cli import kanban_waiting_reader as domain
from hermes_cli import kanban_reader_retention as retention
from hermes_cli import kanban_session_activity as activity
from hermes_cli import sqlite_safe_read as safe
from tests.hermes_cli import test_cached_ro_warm_admission as warm

board = warm.board
held_elsewhere = warm.held_elsewhere


@pytest.fixture(autouse=True)
def domain_cleanup():
    domain.close_all()
    yield
    domain.close_all()
    assert not domain._cache


@pytest.fixture
def isolated_warm(monkeypatch):
    # Reuse the exact retained-custody controls against the independent domain,
    # without monkeypatching activity.cached_ro (the real implementation it calls).
    monkeypatch.setattr(warm, 'activity', SimpleNamespace(
        cached_ro=domain.reading, _RO_CACHE=domain._cache,
        _reset_cache_for_tests=domain.close_all))
    from hermes_cli import kanban_reader_lifecycle as readers
    original = readers.borrow_warm
    monkeypatch.setattr(readers, 'borrow_warm', lambda path, **kw:
                        original(path, **(kw or {'reader_cache': (domain._cache, domain._lock)})))


@pytest.mark.parametrize('invalid', ['poison', 'forgotten', 'tracking', 'shm'])
def test_domain_uncertain_custody(board, monkeypatch, isolated_warm, invalid):
    warm.test_uncertain_warm_entry_keeps_lifecycle_admission(board, monkeypatch, invalid)


def test_domain_lifecycle_and_read_your_writes(board, isolated_warm):
    warm.test_warm_zero_budget_read_during_other_lifecycle_transition(board)


def test_domain_cold_lifecycle(board, isolated_warm):
    warm.test_cold_zero_budget_read_keeps_lifecycle_admission(board)


def test_domain_transaction(board, isolated_warm):
    warm.test_transaction_declines_warm_admission(board)


def test_domain_stale_sibling(board, monkeypatch, isolated_warm):
    warm.test_stale_sibling_declines_warm_admission(board, monkeypatch)


def test_domain_main_replacement(board, tmp_path, isolated_warm):
    warm.test_replaced_main_file_declines_warm_admission(board, tmp_path)


def test_domain_body_error_tracking_custody(board, isolated_warm):
    warm.test_error_cleanup_retains_tracking_until_lifecycle_owner_releases(board)


def test_domain_raw_exclusion(board, isolated_warm):
    warm.test_warm_custody_excludes_raw_probe_without_acquiring_tracking_mutex(board)


def test_domain_cap_lru_and_close_custody(board, tmp_path, monkeypatch):
    monkeypatch.setattr(domain, 'LIMIT', 1)
    with domain.reading(board) as original:
        pass
    other = tmp_path / 'other.db'
    with contextlib.closing(sqlite3.connect(other)) as conn:
        conn.execute('CREATE TABLE t(v)')
    with held_elsewhere(generation.lifecycle_lock(board)):
        with domain.reading(other) as private:
            assert len(domain._cache) == 1
            assert domain._cache[str(board)][0] is original
        with pytest.raises(sqlite3.ProgrammingError):
            private.execute('SELECT 1')
        domain.close_all()
        assert safe.has_live_connection(board)
    with domain.reading(other):
        assert len(domain._cache) == 1
        assert str(board) not in domain._cache
    with pytest.raises(sqlite3.ProgrammingError):
        original.execute('SELECT 1')


def test_domain_body_sqlite_error_retires_entry(board):
    with pytest.raises(sqlite3.OperationalError, match='body failure'):
        with domain.reading(board) as original:
            raise sqlite3.OperationalError('body failure')
    assert not domain._cache
    with pytest.raises(sqlite3.ProgrammingError):
        original.execute('SELECT 1')
    with domain.reading(board) as current:
        assert current is not original
        assert current.execute('SELECT v FROM t').fetchone() == (1,)


def test_domain_budget_restore_failure_retires_entry(board):
    with domain.reading(board, busy_budget=0) as original:
        original.close()  # Restore's PRAGMA must now fail, not the caller's body.
    assert not domain._cache
    with domain.reading(board, busy_budget=0) as current:
        assert current is not original
        assert current.execute('SELECT v FROM t').fetchone() == (1,)


def test_domain_registered_generation_recovery_retires_foreign_reader(board):
    connections, failures = [], []

    def opener():
        try:
            with domain.reading(board) as conn:
                connections.append(conn)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=opener)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert not failures
    conn, = connections
    assert conn._hermes_generation_thread != threading.get_ident()
    conn._hermes_generation_shm = (-1, -1)
    generation.recover(board)  # Use the registry, never domain.evict directly.
    assert not domain._cache
    assert not safe.has_live_connection(board)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')
    with domain.reading(board) as current:
        assert current is not conn
        assert current.execute('SELECT v FROM t').fetchone() == (1,)


def test_domain_lru_refreshes_hit(board, tmp_path, monkeypatch):
    monkeypatch.setattr(domain, 'LIMIT', 2)
    others = [tmp_path / name for name in ('b.db', 'c.db')]
    for path in others:
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE t(v)')
    b, c = others
    with domain.reading(board):
        pass
    with domain.reading(b) as retired:
        pass
    with domain.reading(board):
        pass
    with domain.reading(c):
        pass
    assert list(domain._cache) == [str(board), str(c)]
    with pytest.raises(sqlite3.ProgrammingError):
        retired.execute('SELECT 1')


def test_domain_gone_rechecked_inside_lifecycle(board, monkeypatch):
    from hermes_cli import kanban_sync_board_watch as watch
    with domain.reading(board) as original:
        pass
    answers = iter((True, False))
    calls = []

    def path_gone(path):
        calls.append(path)
        return next(answers)

    monkeypatch.setattr(watch, 'path_gone', path_gone)
    assert domain.release_gone() == 0
    assert calls == [str(board), str(board)]
    assert domain._cache[str(board)][0] is original
    assert original.execute('SELECT v FROM t').fetchone() == (1,)
    assert safe.has_live_connection(board)


def test_domain_warm_statement_budget(board):
    with domain.reading(board) as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone() == (500,)
    with domain.reading(board, busy_budget=0) as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone() == (0,)
    with domain.reading(board) as conn:
        assert conn.execute('PRAGMA busy_timeout').fetchone() == (500,)


def test_gone_domain_reader_is_swept_without_revisit(board):
    with domain.reading(board) as conn:
        board.unlink()
        assert retention.release_gone() == 0  # borrowed; never force close
        assert safe.has_live_connection(board)
    assert retention.release_gone() == 1
    assert not safe.has_live_connection(board)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_domain_cold_tracking_contention_is_not_bypassed(board):
    with held_elsewhere(safe._live_lock):
        with pytest.raises(sqlite3.OperationalError, match='tracking registry is busy'):
            with domain.reading(board, busy_budget=0):
                pytest.fail('cold admission bypassed tracking custody')
    assert not domain._cache
    with domain.reading(board) as conn:
        assert conn.execute('SELECT v FROM t').fetchone() == (1,)


def test_domain_nested_private_and_error_release(board):
    with domain.reading(board) as original:
        with domain.reading(board) as nested:
            assert nested is not original
            assert len(domain._cache) == 1
        with pytest.raises(sqlite3.ProgrammingError):
            nested.execute('SELECT 1')
    with pytest.raises(ValueError, match='caller'):
        with domain.reading(board):
            raise ValueError('caller')
    with domain.reading(board) as current:
        assert current is original


def test_domain_foreign_borrow_private_is_closed(board):
    with held_elsewhere(domain._read_lock):
        with domain.reading(board, busy_budget=0) as private:
            assert private.execute('SELECT v FROM t').fetchone() == (1,)
            assert not domain._cache
        with pytest.raises(sqlite3.ProgrammingError):
            private.execute('SELECT 1')
    assert not safe.has_live_connection(board)


def test_domain_recovery_refuses_borrowed_then_retires(board):
    canonical = generation._resolved(board)
    with domain.reading(board) as conn:
        with pytest.raises(generation.DeletedWalGenerationError):
            domain.evict(canonical)
        assert safe.has_live_connection(board)
    domain.evict(canonical)
    assert not domain._cache
    assert not safe.has_live_connection(board)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_domain_recovery_preserves_unfinished_transaction(board):
    with domain.reading(board) as conn:
        conn.execute('BEGIN')
    try:
        with pytest.raises(generation.DeletedWalGenerationError):
            domain.evict(generation._resolved(board))
        assert conn.in_transaction
        assert safe.has_live_connection(board)
    finally:
        conn.rollback()
    domain.evict(generation._resolved(board))
    assert not domain._cache


def test_domain_recovery_map_contention_is_nonblocking(board):
    with domain.reading(board):
        pass
    with held_elsewhere(domain._lock):
        with pytest.raises(generation.DeletedWalGenerationError, match='reader registry is busy'):
            domain.evict(generation._resolved(board))
    domain.evict(generation._resolved(board))
    assert not domain._cache


def test_domain_alias_recovery(board, tmp_path):
    alias = tmp_path / 'alias.db'
    alias.symlink_to(board)
    with domain.reading(alias) as conn:
        pass
    domain.evict(generation._resolved(board))
    assert not domain._cache
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute('SELECT 1')


def test_generic_prune_keeps_waiting_reader_warm(board, tmp_path, monkeypatch):
    reading = getattr(activity, 'waiting_ro', activity.cached_ro)
    monkeypatch.setattr(retention, 'LIMIT', 1)
    try:
        with reading(board) as original:
            assert original.execute('SELECT v FROM t').fetchone() == (1,)
        for i in range(3):
            other = tmp_path / f'other{i}.db'
            with contextlib.closing(sqlite3.connect(other)) as conn:
                conn.execute('CREATE TABLE t(v)')
            with retention.reader(other):
                pass
        with held_elsewhere(safe._live_lock):
            with reading(board, busy_budget=0) as current:
                assert current is original
                assert current.execute('SELECT v FROM t').fetchone() == (1,)
    finally:
        close = getattr(activity, 'close_waiting_readers', None)
        if close is not None:
            close()
