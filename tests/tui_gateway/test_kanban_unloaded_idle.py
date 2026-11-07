"""Structural idle sweep proofs: unchanged rows do not imply unchanged clocks."""
import contextlib

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from tui_gateway import kanban_unloaded as sweep


def _sub():
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title='idle projection', assignee='worker')
        notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='idle-chat')
    return tid


def test_unchanged_sweep_skips_subscription_and_event_scans(monkeypatch):
    _sub()
    monkeypatch.setattr(sweep, '_board_slugs', lambda kb: ['default'])
    assert sweep.sweep_once(set()) == []
    calls = []
    original = notify.list_notify_subs
    original_events = notify.unseen_events_for_sub
    def listed(*args, **kwargs):
        calls.append('subscriptions')
        return original(*args, **kwargs)
    def events(*args, **kwargs):
        calls.append('events')
        return original_events(*args, **kwargs)
    monkeypatch.setattr(notify, 'list_notify_subs', listed)
    monkeypatch.setattr(notify, 'unseen_events_for_sub', events)
    assert sweep.sweep_once(set()) == []
    assert calls == []


def test_new_event_is_candidate_on_next_sweep_and_retried(monkeypatch):
    tid = _sub()
    monkeypatch.setattr(sweep, '_board_slugs', lambda kb: ['default'])
    reached = []
    monkeypatch.setattr(sweep, '_sweep_sub', lambda conn, slug, sub, *args:
                        reached.append(sub['task_id']))
    sweep.sweep_once(set())
    assert reached == []
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
    sweep.sweep_once(set())
    assert reached == [tid]
    sweep.sweep_once(set())
    assert reached == [tid, tid]  # grace/holder changes need no commit


def test_warm_observer_skips_candidate_query_across_reopened_connections(monkeypatch):
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    _sub()
    with contextlib.closing(kbc.connect()) as conn:
        assert candidates.subscriptions(conn, kinds) == []
    def forbidden(*args):
        raise AssertionError('unchanged candidate SQL ran')
    monkeypatch.setattr(candidates, '_read_candidates', forbidden)
    with contextlib.closing(kbc.connect()) as conn:
        assert candidates.subscriptions(conn, kinds) == []


def test_transaction_and_observer_failure_use_caller(monkeypatch):
    import sqlite3
    from hermes_cli import kanban_reader_retention as retention
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
        @contextlib.contextmanager
        def unavailable(path):
            raise sqlite3.OperationalError('observer unavailable')
            yield
        monkeypatch.setattr(retention, 'reader', unavailable)
        assert [s['task_id'] for s in candidates.subscriptions(conn, kinds)] == [tid]
        conn.execute('BEGIN')
        conn.execute('UPDATE kanban_notify_subs SET last_event_id = (SELECT MAX(id) FROM task_events)')
        assert candidates.subscriptions(conn, kinds) == []
        conn.rollback()
        assert [s['task_id'] for s in candidates.subscriptions(conn, kinds)] == [tid]


def test_transaction_reads_uncommitted_caller_with_available_observer():
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
        assert candidates.subscriptions(conn, kinds)[0]['task_id'] == tid
        conn.execute('BEGIN')
        conn.execute('UPDATE kanban_notify_subs SET last_event_id=(SELECT MAX(id) FROM task_events)')
        assert candidates.subscriptions(conn, kinds) == []
        conn.rollback()
        assert candidates.subscriptions(conn, kinds)[0]['task_id'] == tid


def test_cursor_and_inflight_commits_invalidate_warm_memo():
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
        assert [s['task_id'] for s in candidates.subscriptions(conn, kinds)] == [tid]
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_notify_subs SET last_event_id = (SELECT MAX(id) FROM task_events)')
        assert candidates.subscriptions(conn, kinds) == []
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_notify_subs SET inflight_from = 0')
        assert [s['task_id'] for s in candidates.subscriptions(conn, kinds)] == [tid]
        with kb.write_txn(conn):
            conn.execute('DELETE FROM kanban_notify_subs')
        assert candidates.subscriptions(conn, kinds) == []


def test_warm_claim_recovers_when_holder_dies(monkeypatch):
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    monkeypatch.setattr(sweep, '_board_slugs', lambda kb: ['default'])
    monkeypatch.setattr(notify, '_holder_is_me', lambda row: False)
    alive = [True]
    monkeypatch.setattr(notify, '_holder_alive', lambda row: alive[0])
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_notify_subs SET inflight_from=0, inflight_pid=123, '
                         'last_event_id=(SELECT MAX(id) FROM task_events)')
        assert candidates.subscriptions(conn, kinds)[0]['inflight_from'] == 0
    reached = []
    def observe(conn, slug, sub, *args):
        reached.append((sub['inflight_from'], sub['last_event_id']))
    monkeypatch.setattr(sweep, '_sweep_sub', observe)
    sweep.sweep_once(set())
    assert reached[-1][0] == 0
    alive[0] = False  # no database commit; recovery must still run
    sweep.sweep_once(set())
    assert reached[-1] == (None, 0)


def test_drained_and_deleted_stalls_clear_without_affecting_other_boards(monkeypatch):
    tid = _sub()
    monkeypatch.setattr(sweep, '_board_slugs', lambda kb: ['default'])
    monkeypatch.setattr(sweep, '_sweep_sub', lambda *args: None)
    key = ('default', tid, 'idle-chat')
    other = ('other', tid, 'idle-chat')
    monkeypatch.setattr(sweep, '_stalls', {key: 'busy', other: 'busy'})
    monkeypatch.setattr(sweep, '_overdue_warned', {key, other})
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
        sweep.sweep_once({'idle-chat'})
        assert key in sweep._stalls  # live positive candidate stays tracked
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_notify_subs SET last_event_id=(SELECT MAX(id) FROM task_events)')
        sweep.sweep_once(set())
        assert sweep._stalls == {other: 'busy'}
        assert sweep._overdue_warned == {other}
        sweep._stalls[key] = 'busy'
        sweep._overdue_warned.add(key)
        with kb.write_txn(conn):
            conn.execute('DELETE FROM kanban_notify_subs')
        sweep.sweep_once(set())
        assert sweep._stalls == {other: 'busy'}
        assert sweep._overdue_warned == {other}


def test_candidate_rows_match_original_projection_and_are_independent():
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    with contextlib.closing(kbc.connect()) as conn:
        for platform, chat, thread in [('TuI', 'mixed', 'thread'), ('tui', '', ''), ('slack', 'foreign', '')]:
            notify.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat, thread_id=thread)
        kb.complete_task(conn, tid, summary='ready')
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_notify_subs SET delivery_metadata=?', ('{"nested":{"value":1}}',))
        expected = [s for s in notify.list_notify_subs(conn)
                    if s['platform'].lower() == 'tui' and s['chat_id']]
        rows = candidates.subscriptions(conn, kinds)
        assert sorted(rows, key=lambda s: s['chat_id']) == sorted(expected, key=lambda s: s['chat_id'])
        rows[0]['delivery_metadata']['test_mutation'] = 2
        assert 'test_mutation' not in candidates.subscriptions(conn, kinds)[0]['delivery_metadata']


def test_kind_key_and_same_connection_writes_are_visible():
    from tui_gateway import kanban_unloaded_candidates as candidates
    tid = _sub()
    with contextlib.closing(kbc.connect()) as conn:
        kb.complete_task(conn, tid, summary='ready')
        assert candidates._on_connection(conn, ('unrelated-kind',)) == []
        assert candidates._on_connection(conn, ())[0]['task_id'] == tid
        assert [s['task_id'] for s in candidates._on_connection(conn, ('completed',))] == [tid]
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_notify_subs SET last_event_id=(SELECT MAX(id) FROM task_events)')
        assert candidates._on_connection(conn, ('completed',)) == []


def test_commit_during_scan_is_not_cached_under_new_version(monkeypatch):
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    original = candidates._read_candidates
    def raced(conn, requested):
        rows = original(conn, requested)
        with contextlib.closing(kbc.connect()) as writer:
            kb.complete_task(writer, tid, summary='ready')
        return rows
    with contextlib.closing(kbc.connect()) as conn:
        with monkeypatch.context() as m:
            m.setattr(candidates, '_read_candidates', raced)
            assert candidates.subscriptions(conn, kinds) == []
        assert [s['task_id'] for s in candidates.subscriptions(conn, kinds)] == [tid]


def test_replaced_database_does_not_reuse_old_candidates(tmp_path):
    import sqlite3
    from hermes_cli import kanban_reader_retention as retention
    from tui_gateway import kanban_unloaded_candidates as candidates
    from tui_gateway.session_notifications import _KANBAN_NOTIFY_KINDS as kinds
    tid = _sub()
    with contextlib.closing(kbc.connect()) as conn:
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        assert candidates.subscriptions(conn, kinds) == []
        replacement = tmp_path / 'replacement.db'
        with contextlib.closing(sqlite3.connect(replacement)) as copy:
            conn.backup(copy)
            copy.execute('UPDATE kanban_notify_subs SET inflight_from=0')
            copy.commit()
    # Release the observer before replacing the file, as board rebuild does.
    retention.close_all()
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    replacement.replace(path)
    with contextlib.closing(kbc.connect()) as conn:
        assert [s['task_id'] for s in candidates.subscriptions(conn, kinds)] == [tid]
