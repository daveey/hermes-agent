"""Parked graph reads hand Python only edges from unfinished parents."""
import contextlib
import sqlite3

from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_session_activity as base

kanban_root = base.kanban_root


def test_parked_graph_omits_finished_parent_rows_but_keeps_transitive_holds(kanban_root):
    path = kanban_root / 'kanban.db'
    rows = [('ask', 'blocked', None), ('mid', 'todo', None),
            ('leaf', 'todo', 'held-chat'), ('cycle', 'todo', None),
            ('running', 'running', None), ('waiting', 'todo', 'parked-chat'),
            ('free', 'todo', 'free-chat')]
    rows += [(f'done-{i}', 'done' if i % 2 else 'archived', None) for i in range(1200)]
    base._make_board(path, rows)
    base._add_human_requests(path, 'ask')
    base._add_links(path, [('ask', 'mid'), ('mid', 'leaf'), ('mid', 'cycle'),
                           ('cycle', 'mid'), ('running', 'waiting'),
                           ('missing', 'free')] + [(f'done-{i}', 'free') for i in range(1200)])
    handed = []
    with contextlib.closing(ksa._open_ro(path)) as db:
        def record(cursor, row):
            if tuple(col[0] for col in cursor.description) == ('parent_id', 'child_id'):
                handed.append(tuple(row))
            return sqlite3.Row(cursor, row)
        db.row_factory = record
        assert ksa._parked_on(db) == ({'parked-chat': 1}, {'held-chat': 1})
        assert set(handed) == {('ask', 'mid'), ('mid', 'leaf'), ('mid', 'cycle'),
                               ('cycle', 'mid'), ('running', 'waiting')}
        assert len(handed) == 5

    # Next pass reads current status; no cross-call memo can hide a newly open parent.
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE tasks SET status='running' WHERE id='done-0'")
    with contextlib.closing(ksa._open_ro(path)) as db:
        assert ksa._parked_on(db) == ({'parked-chat': 1, 'free-chat': 1}, {'held-chat': 1})


def test_parent_edge_selection_handles_empty_and_large_bound_id_sets():
    from hermes_cli.kanban_session_activity_parked import open_parent_edges

    with contextlib.closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE task_links(parent_id TEXT, child_id TEXT, PRIMARY KEY(parent_id,child_id))')
        ids = [f'open-{i}' for i in range(1201)] + ["quote';雪"]
        db.executemany('INSERT INTO task_links VALUES (?,?)', [(parent, 'child') for parent in ids])
        assert open_parent_edges(db, []) == {}
        selected = open_parent_edges(db, ids)
        assert sorted(selected['child']) == sorted(ids)
        assert open_parent_edges(db, [ids[-1]]) == {'child': [ids[-1]]}
