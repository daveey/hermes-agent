"""PR4045 review on 8d09c5ee: the guard's lexer must tokenize like SQLite itself.

P1: SQLite treats U+FEFF (BOM) as whitespace, so ``REPLACE INTO \ufefftasks`` targets ``tasks``.
P2: SQLite treats other non-ASCII characters as identifier characters, so ``tasksé`` is another table.
Each case is checked against real SQLite first (control), then against the guard.
"""
import sqlite3

import pytest

from hermes_cli.kanban_replace_guard import ProtectedRowReplaceError, reject_replace

BOM = '\ufeff'
PROTECTED = [
    f'REPLACE INTO {BOM}tasks VALUES (1)',
    f'INSERT OR REPLACE INTO {BOM}task_runs VALUES (1)',
    f'REPLACE INTO{chr(10)}{BOM}tasks VALUES (1)',
    f'{BOM}INSERT OR REPLACE INTO {BOM}{BOM}tasks VALUES (1)',
    f'UPDATE OR REPLACE {BOM}tasks SET id = 1',
    f'REPLACE INTO main.{BOM}tasks VALUES (1)',
]
UNRELATED = [
    'REPLACE INTO tasksé VALUES (1)',
    'INSERT OR REPLACE INTO task_runsé VALUES (1)',
    'REPLACE INTO étasks VALUES (1)',
    'UPDATE OR REPLACE tasks_é SET id = 1',
    f'REPLACE INTO tasks{BOM} VALUES (1)',  # a trailing BOM is part of the identifier
]


def _raw():
    db = sqlite3.connect(':memory:')
    for name in ('tasks', 'task_runs', 'tasksé', 'task_runsé', 'étasks', 'tasks_é', 'tasks' + BOM):
        db.execute(f'CREATE TABLE "{name}" (id PRIMARY KEY)')
    return db


@pytest.mark.parametrize('sql', PROTECTED)
def test_bom_whitespace_is_still_a_protected_replace(sql):
    db = _raw()
    db.execute('INSERT INTO tasks VALUES (2)')
    before = db.total_changes
    db.execute(sql)  # control: SQLite accepts the spelling and writes a protected table
    assert db.total_changes > before
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] + \
        db.execute('SELECT count(*) FROM task_runs').fetchone()[0] >= 2 or sql.startswith('UPDATE')
    with pytest.raises(ProtectedRowReplaceError):
        reject_replace(sql)


@pytest.mark.parametrize('sql', UNRELATED)
def test_non_ascii_suffix_is_a_different_table(sql):
    db = _raw()
    db.execute('INSERT INTO "tasks_é" VALUES (1)')
    db.execute(sql)  # control: SQLite runs it against the unrelated table
    assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
    reject_replace(sql)  # no false positive
