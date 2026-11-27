"""Real-SQL notification observer contention and lifetime regressions."""
import contextlib
import threading
import time

from hermes_cli import kanban_session_activity as activity
from tests.tui_gateway import test_notify_subs_gate as helpers
from tui_gateway import notification_idle as idle

board = helpers.board


def test_generic_reader_contention_keeps_warm_notification_work_bounded(board, monkeypatch):
    path, _tasks = board
    # Trace actual connections at open, including ephemeral private fallbacks.
    from hermes_cli import kanban_generation as generation
    original = generation.open_ro
    statements = []

    def traced(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(generation, 'open_ro', traced)
    monkeypatch.setattr(activity, '_open_ro', traced)
    before = idle.board_signature(path, helpers.CHAT)
    statements.clear()
    ready, release = threading.Event(), threading.Event()

    def hold():
        with activity.cached_ro(path):
            ready.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        for _ in range(4):
            assert idle.board_signature(path, helpers.CHAT) == before
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    scans = [s for s in statements if idle._SUBS_SQL in s]
    zero = [s for s in statements if 'FROM task_events WHERE id > 0 ' in s]
    assert (len(scans), len(zero)) == (0, 0), statements


def test_generic_hold_still_sees_commits_without_cold_event_replay(board):
    from tests.tui_gateway.test_notify_revision import writer
    from tui_gateway import notification_reader as reader
    path, tasks = board
    before = idle.board_signature(path, helpers.CHAT)
    with reader.reading(path) as (conn, _):
        statements = []
        conn.set_trace_callback(statements.append)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with activity.cached_ro(path):
            ready.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5)
        with writer(path) as conn:
            conn.execute("UPDATE kanban_notify_subs SET last_event_id = 96 WHERE chat_id = ?", (helpers.CHAT,))
            conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, 'completed', '{}', 1)",
                         (tasks[0],))
        after = idle.board_signature(path, helpers.CHAT)
        assert after != before and after[1] > before[1]
        assert after[2][0][4] == 96
        for _ in range(3):
            assert idle.board_signature(path, helpers.CHAT) == after
        assert sum(idle._SUBS_SQL in s for s in statements) == 1
        assert not any('FROM task_events WHERE id > 0 ' in s for s in statements)
    finally:
        release.set()
        thread.join(5)
        with reader.reading(path) as (conn, _):
            conn.set_trace_callback(None)
    assert not thread.is_alive()


def test_reset_during_borrow_defers_close_and_prevents_reuse(board):
    import sqlite3
    import pytest
    from tui_gateway import notification_reader as reader
    path, _ = board
    with reader.reading(path) as (old, retained):
        assert retained
        idle.reset_board_signatures()
        assert old.execute('SELECT 1').fetchone() == (1,)
        with reader.reading(path) as (fresh, retained):
            assert not retained and fresh is not old
    with pytest.raises(sqlite3.ProgrammingError):
        old.execute('SELECT 1')


def test_private_reader_closes_on_exception_and_never_publishes(board):
    import sqlite3
    import pytest
    from tui_gateway import notification_reader as reader
    path, _ = board
    idle.board_signature(path, helpers.CHAT)
    held = idle._SIGS[str(path)]
    with reader.reading(path) as (shared, _):
        with pytest.raises(ValueError, match='controlled'):
            with reader.reading(path, busy_budget=0) as (private, retained):
                assert not retained and private is not shared
                assert private.execute('PRAGMA query_only').fetchone() == (1,)
                raise ValueError('controlled')
        with pytest.raises(sqlite3.ProgrammingError):
            private.execute('SELECT 1')
        assert idle._SIGS[str(path)] is held
        assert shared.execute('SELECT 1').fetchone() == (1,)


def test_error_and_unfinished_transaction_close_retained_reader(board):
    import sqlite3
    import pytest
    from tui_gateway import notification_reader as reader
    path, _ = board
    with pytest.raises(sqlite3.OperationalError):
        with reader.reading(path) as (old, _):
            old.execute('SELECT * FROM nonexistent_notification_fixture')
    with pytest.raises(sqlite3.ProgrammingError):
        old.execute('SELECT 1')
    with reader.reading(path) as (transaction, _):
        transaction.execute('BEGIN')
    with pytest.raises(sqlite3.ProgrammingError):
        transaction.execute('SELECT 1')
    assert idle.board_signature(path, helpers.CHAT)[0] == 'notify'


def test_generation_eviction_refuses_foreign_borrow_and_reopens(board):
    import sqlite3
    import pytest
    from hermes_cli import kanban_generation as generation
    from tui_gateway import notification_reader as reader
    path, _ = board
    errors = []
    with reader.reading(path) as (old, _):
        def evict():
            try:
                reader.evict(str(path.resolve()))
            except generation.DeletedWalGenerationError as exc:
                errors.append(exc)
        thread = threading.Thread(target=evict)
        thread.start()
        thread.join(5)
        assert not thread.is_alive() and len(errors) == 1
        assert old.execute('SELECT 1').fetchone() == (1,)
    reader.evict(str(path.resolve()))
    with reader.reading(path) as (fresh, _):
        assert fresh is not old
    # The registered callback, not only its direct helper, participates in recovery.
    assert reader.evict in list(generation._reader_caches)
    with pytest.raises(sqlite3.ProgrammingError) as caught:
        old.execute('SELECT 1')
    assert 'closed' in str(caught.value)


def test_registry_cap_closes_idle_and_active_retirees(board, monkeypatch):
    import sqlite3
    import pytest
    from tui_gateway import notification_reader as reader
    path, _ = board
    other = path.with_name('other-notify.db')
    with contextlib.closing(sqlite3.connect(path)) as source:
        with contextlib.closing(sqlite3.connect(other)) as target:
            source.backup(target)
    monkeypatch.setattr(reader, 'MAX_READERS', 1)
    with reader.reading(path) as (old, _):
        pass
    with reader.reading(other) as (second, _):
        with pytest.raises(sqlite3.ProgrammingError):
            old.execute('SELECT 1')
        started = time.monotonic()
        with reader.reading(path) as (third, retained):
            assert not retained  # active slots are never evicted to make room
            assert second.execute('SELECT 1').fetchone() == (1,)
            assert third is not old
        assert time.monotonic() - started < 1, 'capacity eviction waited on a borrowed reader'
    reader.close_all()
    with pytest.raises(sqlite3.ProgrammingError):
        second.execute('SELECT 1')
    assert len(reader._registry) == 0


def test_wal_only_replacement_invalidates_observer(board):
    import sqlite3
    from pathlib import Path
    path, _ = board
    before = idle.board_signature(path, helpers.CHAT)
    old = idle._SIGS[str(path)][0]
    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    # Only WAL changes: same main inode and same SHM mapping.
    wal = Path(str(path) + '-wal')
    wal.rename(Path(str(path) + '-old-wal'))
    wal.touch()
    assert idle.board_signature(path, helpers.CHAT) == before
    assert idle._SIGS[str(path)][0] is not old


def test_shm_only_replacement_invalidates_observer(board):
    from pathlib import Path
    path, _ = board
    before = idle.board_signature(path, helpers.CHAT)
    old = idle._SIGS[str(path)][0]
    Path(str(path) + '-shm').rename(Path(str(path) + '-old-shm'))
    assert idle.board_signature(path, helpers.CHAT) == before
    assert idle._SIGS[str(path)][0] is not old


def test_reset_deferred_by_foreign_lifecycle_reopens_retired_reader(board):
    from hermes_cli import kanban_generation as generation
    from tui_gateway import notification_reader as reader
    path, _ = board
    before = idle.board_signature(path, helpers.CHAT)
    old = idle._SIGS[str(path)][0]
    done = threading.Event()
    errors = []

    def reset():
        try:
            reader.close_all()
        except generation.BoardLifecycleBusy as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=reset)
    try:
        with generation.lifecycle_access(path, timeout=1):
            thread.start()
            assert done.wait(1), 'reset must defer rather than wait for foreign lifecycle'
            assert reader._registry[str(path)].conn is old
            assert reader._registry[str(path)].retired
    finally:
        thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert idle.board_signature(path, helpers.CHAT) == before
    assert idle._SIGS[str(path)][0] is not old


def test_capacity_retirement_does_not_wait_for_foreign_board_lifecycle(board, monkeypatch):
    from hermes_cli import kanban_generation as generation
    from tui_gateway import notification_reader as reader
    path, _ = board
    with reader.reading(path):
        pass
    other = path.with_name('new-notify.db')
    import sqlite3
    with contextlib.closing(sqlite3.connect(path)) as source:
        with contextlib.closing(sqlite3.connect(other)) as target:
            source.backup(target)
    monkeypatch.setattr(reader, 'MAX_READERS', 1)
    done = threading.Event()
    outcomes = []

    def read_other():
        try:
            with reader.reading(other, busy_budget=0) as (conn, retained):
                outcomes.append((conn.execute('SELECT 1').fetchone(), retained))
        except generation.BoardLifecycleBusy:
            outcomes.append('busy')
        finally:
            done.set()

    thread = threading.Thread(target=read_other)
    try:
        with generation.lifecycle_access(path, timeout=1):
            thread.start()
            assert done.wait(1), 'new board waited for an unrelated lifecycle during capacity retirement'
    finally:
        thread.join(5)
    assert not thread.is_alive()
    assert outcomes == [((1,), False)]


def test_detached_waiting_borrower_is_private_and_closes(board, monkeypatch):
    import sqlite3
    import pytest
    from tui_gateway import notification_reader as reader
    path, _ = board
    with reader.reading(path) as (old, retained):
        assert retained
    slot = reader._registry[str(path)]
    selected, proceed = threading.Event(), threading.Event()
    original = reader._reader
    outcomes = []
    errors = []

    def select(key):
        result = original(key)
        selected.set()
        assert proceed.wait(5)
        return result

    def borrow():
        try:
            with reader.reading(path) as (conn, retained):
                outcomes.append((conn, retained, conn.execute('SELECT 1').fetchone()))
        except (sqlite3.Error, AssertionError) as exc:
            errors.append(exc)

    monkeypatch.setattr(reader, '_reader', select)
    slot.lock.acquire()
    thread = threading.Thread(target=borrow)
    try:
        thread.start()
        assert selected.wait(5)
        with reader._registry_lock:
            assert reader._registry.pop(str(path)) is slot
    finally:
        slot.lock.release()
        proceed.set()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == []
    assert outcomes == [(old, False, (1,))]
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        old.execute('SELECT 1')
    assert slot.conn is None


def test_atomic_replacement_with_retained_reader_rebuilds_equal_revision(tmp_path):
    import sqlite3
    from tui_gateway import notification_reader as reader
    path = tmp_path / 'board.db'
    replacement = tmp_path / 'replacement.db'
    for file, value in ((path, 1), (replacement, 2)):
        with contextlib.closing(sqlite3.connect(file)) as conn:
            conn.executescript('CREATE TABLE task_events(id INTEGER PRIMARY KEY, kind TEXT);'
                               'INSERT INTO task_events VALUES (1, "completed");'
                               'CREATE TABLE kanban_notify_subs(chat_id, task_id, platform, thread_id, '
                               'last_event_id, inflight_from);')
            conn.execute("INSERT INTO kanban_notify_subs VALUES ('chat', 'task', 'tui', '', ?, NULL)", (value,))
            conn.commit()
    try:
        before = idle.board_signature(path, 'chat')
        old = idle._SIGS[str(path)][0]
        replacement.replace(path)  # old observer remains open until the next borrow
        after = idle.board_signature(path, 'chat')
        assert after != before and after[2][0][4] == 2
        assert idle._SIGS[str(path)][0] is not old
    finally:
        reader.close_all()
        idle.reset_board_signatures()
