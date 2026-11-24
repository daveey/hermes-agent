"""History grouping must not sort wide payloads in SQLite on every cold board read."""
import sqlite3
import time

import pytest

from plugins.kanban.dashboard import board_read


class ObservedConnection(sqlite3.Connection):
    def execute(self, sql, parameters=()):
        if sql.startswith('SELECT'):
            plan = super().execute('EXPLAIN QUERY PLAN ' + sql, parameters).fetchall()
            assert not any('TEMP B-TREE' in r[3] for r in plan), 'wide history payload sorter'
        return super().execute(sql, parameters)


@pytest.mark.parametrize('table', ['task_events', 'task_runs'])
def test_grouped_history_does_not_sort_payloads(table):
    with sqlite3.connect(':memory:', factory=ObservedConnection) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, task_id TEXT, created_at, payload)')
        conn.execute(f'CREATE INDEX by_task ON {table}(task_id, created_at)')
        # Arrival IDs and timestamps disagree; grouping must still be ID ordered.
        values = [(9, 'a', 1, 'nine'), (2, 'a', 9, 'two'), (5, 'b', None, 'five')]
        conn.executemany(f'INSERT INTO {table} VALUES (?, ?, ?, ?)', values)
        result = board_read._rows_by_task(conn, table, ['b', 'a', 'missing'])
        assert list(result) == ['b', 'a', 'missing']
        assert [r['id'] for r in result['a']] == [2, 9]
        assert [r['payload'] for r in result['a']] == ['two', 'nine']
        assert [r['id'] for r in result['b']] == [5]
        assert result['missing'] == ()
        with pytest.raises(TypeError):
            result['a'][0]['payload'] = 'mutated'
        assert board_read._rows_by_task(conn, table, []) == {}


def test_live_sized_history_fixture_finishes_inside_board_budget():
    # Public synthetic counterpart of the measured 257-card / 81,123-event,
    # 18.5MB history selection. Setup is deliberately outside the timing guard.
    with sqlite3.connect(':memory:', factory=ObservedConnection) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, created_at, payload)')
        conn.execute('CREATE INDEX by_task ON task_events(task_id, created_at)')
        ids = [f't-{i}' for i in range(257)]
        conn.executemany('INSERT INTO task_events VALUES (?, ?, ?, ?)',
                         ((i, ids[i % len(ids)], -i, 'x' * 228) for i in range(81123)))
        start = time.perf_counter()
        result = board_read._rows_by_task(conn, 'task_events', ids)
        elapsed = time.perf_counter() - start
        assert elapsed < 2, f'history stage alone exhausted the board budget: {elapsed:.3f}s'
        assert sum(map(len, result.values())) == 81123
        assert all([r['id'] for r in group] == sorted(r['id'] for r in group)
                   for group in result.values())
