"""Real board SQL capture must cross the frozen owner boundary without Task objects."""
import time

import pytest

from hermes_cli import kanban_db, kanban_db_connect
from hermes_cli.fact_contract import freeze
from plugins.kanban.dashboard import board_read


def test_plain_capture_matches_legacy_and_detaches(tmp_path, monkeypatch):
    from plugins.kanban.dashboard import board_fact_capture

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    kanban_db.init_db()
    with kanban_db_connect.connect() as conn:
        parent = kanban_db.create_task(conn, title='parent', assignee='default')
        child = kanban_db.create_task(conn, title='child', assignee='default')
        kanban_db.link_tasks(conn, parent, child)
        kanban_db.add_comment(conn, parent, 'fixture', 'comment')
        conn.commit()
        conn.execute('BEGIN')
        rows = board_read.materialize(conn, None, True, None, None, 80)
        expected = board_read.project(rows, True)
        captured = board_fact_capture.capture(conn, include_archived=True)
        conn.rollback()
    frozen = freeze(captured)

    def forbidden(*args, **kwargs):
        pytest.fail('rendering revisited a capture dependency')

    with monkeypatch.context() as guard:
        guard.setattr(board_read, 'materialize', forbidden)
        guard.setattr(board_read, 'diagnostics_from_inputs', forbidden)
        guard.setattr(kanban_db, 'task_age', forbidden)
        guard.setattr(kanban_db, 'list_profiles_on_disk', forbidden)
        guard.setattr(time, 'time', forbidden)
        actual = board_fact_capture.project(frozen)
        assert actual == expected
        cards = [t for c in actual['columns'] for t in c['tasks']]
        next(t for t in cards if t['id'] == parent)['link_counts']['children'] = 999
        assert board_fact_capture.project(frozen) == expected

    with kanban_db_connect.connect() as conn:
        conn.execute('DELETE FROM task_links WHERE child_id=?', (child,))
        conn.execute('DELETE FROM tasks WHERE id=?', (child,))
        conn.commit()
        conn.execute('BEGIN')
        changed = board_fact_capture.capture(conn, include_archived=True)
        conn.rollback()
    actual = board_fact_capture.project(freeze(changed))
    cards = [t for c in actual['columns'] for t in c['tasks']]
    assert [t['id'] for t in cards] == [parent]
    assert cards[0]['link_counts']['children'] == 0
    assert board_fact_capture.project(frozen) == expected


def test_capture_retains_sql_snapshot_across_writer(tmp_path, monkeypatch):
    from plugins.kanban.dashboard import board_fact_capture

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    kanban_db.init_db()
    with kanban_db_connect.connect() as conn:
        tid = kanban_db.create_task(conn, title='before', assignee='default')
        conn.commit()
        original = board_read.diagnostic_inputs
        wrote = False

        def interleave(reader, task_ids=None):
            nonlocal wrote
            if not wrote:
                wrote = True
                with kanban_db_connect.connect() as writer:
                    writer.execute('UPDATE tasks SET title=? WHERE id=?', ('after', tid))
                    kanban_db.add_comment(writer, tid, 'fixture', 'concurrent')
                    writer.commit()
            return original(reader, task_ids)

        original_materialize = board_read.materialize

        def materialize(*args, **kwargs):
            return original_materialize(*args, **kwargs, diagnostic_inputs=interleave)

        with monkeypatch.context() as guard:
            guard.setattr(board_read, 'materialize', materialize)
            conn.execute('BEGIN')
            before = board_fact_capture.project(freeze(board_fact_capture.capture(conn)))
            conn.rollback()
        conn.execute('BEGIN')
        after = board_fact_capture.project(freeze(board_fact_capture.capture(conn)))
        conn.rollback()
    old = next(t for c in before['columns'] for t in c['tasks'])
    new = next(t for c in after['columns'] for t in c['tasks'])
    assert wrote
    assert (old['title'], old['comment_count']) == ('before', 0)
    assert (new['title'], new['comment_count']) == ('after', 1)


def test_capture_requires_caller_transaction(tmp_path, monkeypatch):
    from plugins.kanban.dashboard import board_fact_capture

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    kanban_db.init_db()
    with kanban_db_connect.connect() as conn:
        assert not conn.in_transaction
        with pytest.raises(ValueError, match='transaction'):
            board_fact_capture.capture(conn)
