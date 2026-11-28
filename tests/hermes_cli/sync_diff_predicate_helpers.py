"""Reference readers intentionally retain the pre-optimization statements."""
import sqlite3

import pytest


TABLES = ('task_links', 'task_comments', 'task_runs', 'task_chat_links')


def original(conn, table, task_ids, gateway=None):
    if table == 'task_links':
        return {(p, f'{p}>{c}') for p, c in conn.execute('SELECT parent_id, child_id FROM task_links') if c in task_ids}
    if table == 'task_chat_links' and gateway is not None:
        rows = conn.execute(
            "SELECT l.task_id, l.uid FROM task_chat_links l LEFT JOIN tasks t ON t.id = l.task_id WHERE l.uid IS NOT NULL "
            "AND NOT (t.created_on IS NOT NULL AND t.created_on != ?1 AND COALESCE(l.chat_gateway, '') != ?1)", (gateway,))
        return {(t, u) for t, u in rows if t in task_ids}
    return {(t, u) for t, u in conn.execute(f'SELECT task_id, uid FROM {table} WHERE uid IS NOT NULL') if t in task_ids}


@pytest.fixture
def inventory():
    c = sqlite3.connect(':memory:')
    c.executescript('''
        CREATE TABLE tasks(id TEXT COLLATE NOCASE, created_on TEXT);
        CREATE TABLE task_links(parent_id TEXT, child_id TEXT COLLATE NOCASE);
        CREATE TABLE task_comments(task_id TEXT COLLATE NOCASE, uid TEXT);
        CREATE TABLE task_runs(task_id TEXT COLLATE NOCASE, uid TEXT);
        CREATE TABLE task_chat_links(task_id TEXT COLLATE NOCASE, uid TEXT, chat_gateway TEXT);
    ''')
    ids = ['A', 'a', '', 'x\x00z', 'quote\'"', 'Ω%', ' space ', 'foreign', 'orphan']
    for i, tid in enumerate(ids):
        if tid != 'orphan':
            c.execute('INSERT INTO tasks VALUES (?,?)', (tid, ('mine', 'other', None)[i % 3]))
        c.execute('INSERT INTO task_links VALUES (?,?)', (None if i == 2 else 'parent', tid))
        for table in ('task_comments', 'task_runs'):
            c.executemany(f'INSERT INTO {table} VALUES (?,?)', [(tid, f'u-{i}'), (tid, f'u-{i}'), (tid, None)])
        for gw in ('mine', 'other', None, ''):
            c.executemany('INSERT INTO task_chat_links VALUES (?,?,?)', [(tid, f'u-{i}', gw), (tid, None, gw)])
    for table in ('task_comments', 'task_runs', 'task_chat_links'):
        c.execute(f'INSERT INTO {table}(task_id,uid) VALUES (NULL,?)', ('null-parent',))
    c.execute('INSERT INTO task_links VALUES (?,NULL)', ('null-child',))
    c.commit()
    c.row_factory = sqlite3.Row
    try:
        yield c, ids
    finally:
        c.close()
