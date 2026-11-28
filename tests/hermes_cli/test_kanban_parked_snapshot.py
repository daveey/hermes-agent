"""Parked graph derivation must not retain the source WAL snapshot."""
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_annotation_host as helpers


@pytest.mark.parametrize('entrypoint', ['batch', 'standalone'])
def test_unowned_ancestor_predicate_runs_after_source_close(tmp_path, monkeypatch, entrypoint):
    path = tmp_path / 'kanban.db'
    writer = helpers._board(path, [('child', 'todo', 's1'), ('ancestor', 'blocked', None)])
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('ALTER TABLE tasks ADD COLUMN human_request TEXT')
    writer.execute("UPDATE tasks SET human_request='ancestor-input' WHERE id='ancestor'")
    writer.execute('CREATE TABLE task_links (parent_id TEXT, child_id TEXT)')
    writer.execute("INSERT INTO task_links VALUES ('ancestor','child')")
    opened, checked = [], []
    original_open = activity._open_ro
    original_predicate = activity.card_is_open_human_ask

    def open_one(path):
        conn = original_open(path)
        opened.append(conn)
        return conn

    def classify(status, kind, request):
        if request == 'ancestor-input':
            assert len(opened) == 1
            with pytest.raises(sqlite3.ProgrammingError, match='closed'):
                opened[0].execute('SELECT 1')
            # Real independent writer/checkpoint can advance while Python
            # classification runs. This checkpoint is fixture-only, never a fix.
            writer.execute("INSERT INTO tasks VALUES ('later','running','s2',NULL)")
            busy, frames, backfilled = writer.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
            assert not busy and frames == backfilled
            checked.append(request)
        return original_predicate(status, kind, request)

    monkeypatch.setattr(activity, '_open_ro', open_one)
    monkeypatch.setattr(activity, 'card_is_open_human_ask', classify)
    try:
        if entrypoint == 'batch':
            reads, complete = activity._read_board(path, parked=True, stalled=False)
            assert complete and reads['active'] == {'s1': 1}
            assert reads['parked'] == ({'s1': 1}, {})
            assert reads['parked_pairs'] == {('s1', 'child')}
        else:
            assert activity._read_parked(path) == ({'s1': 1}, {})
        assert checked == ['ancestor-input']
    finally:
        writer.close()


def test_board_parked_uses_one_snapshot_before_detached_derivation(tmp_path, monkeypatch):
    from tests.hermes_cli import test_kanban_session_activity as base

    path = tmp_path / 'kanban.db'
    base._make_board(path, [('ask', 'blocked', None), ('middle', 'todo', None),
                            ('child', 'todo', 's1'), ('cycle', 'todo', None)])
    base._add_human_requests(path, 'ask')
    base._add_links(path, [('ask', 'middle'), ('middle', 'child'),
                           ('middle', 'cycle'), ('cycle', 'middle')])
    original = activity._derive_parked
    changed = []

    def mutate_then_derive(inputs):
        if not changed:
            with sqlite3.connect(path) as writer:
                writer.execute("UPDATE tasks SET status='done' WHERE id='ask'")
                writer.execute("DELETE FROM task_links WHERE child_id='child'")
            changed.append(True)
        return original(inputs)

    monkeypatch.setattr(activity, '_derive_parked', mutate_then_derive)
    before, complete = activity._read_board(path, parked=True, stalled=False)
    assert complete and before['active'] == {'s1': 1}
    assert before['parked'] == ({}, {'s1': 1})
    assert before['held_pairs'] == {('s1', 'child')}
    after, complete = activity._read_board(path, parked=True, stalled=False)
    assert complete and after['active'] == {'s1': 1}
    assert after['parked'] == ({}, {})
    assert after['held_pairs'] == set()
