"""An isolated starvation index must not reorder other run consumers."""
import sqlite3
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_admission_report as report


def test_s1_planner_diagnostic(tmp_path):
    results = []
    for analyze in (False, True):
        for running in (False, True):
            arms = []
            for indexed in (False, True):
                conn = sqlite3.connect(':memory:')
                conn.row_factory = sqlite3.Row
                conn.executescript(kb.SCHEMA_SQL)
                kbc._migrate_add_optional_columns(conn)
                if not indexed:
                    conn.execute('DROP INDEX idx_runs_started_at')
                conn.executemany('INSERT INTO tasks(id,title,status,created_at) VALUES (?,?,?,?)',
                                 [(f't{i}', 'fixture', 'done', 0) for i in range(1000)])
                conn.executemany('INSERT INTO task_runs(task_id,status,started_at) VALUES (?,?,?)',
                                 [(f't{i % 1000}', 'running' if running else 'done', (4000-i)*100) for i in range(4000)])
                if analyze:
                    conn.execute('ANALYZE')
                query = ("SELECT r.id, r.task_id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
                         "WHERE r.status = 'running' AND r.ended_at IS NULL AND r.started_at < ? "
                         "AND (t.current_run_id IS NULL OR t.current_run_id != r.id)")
                rows = [tuple(r) for r in conn.execute(query, (1000,))]
                plan = [tuple(r) for r in conn.execute('EXPLAIN QUERY PLAN '+query, (1000,))]
                waits = report._waits(conn, 0, 1000, 'absent')
                arms.append({'indexed': indexed, 'dangling': rows, 'plan': plan, 'waits': waits})
                conn.close()
            results.append({'analyze': analyze, 'running': running, 'arms': arms,
                            'dangling_equal': arms[0]['dangling'] == arms[1]['dangling'],
                            'waits_equal': arms[0]['waits'] == arms[1]['waits']})
    assert all(r['dangling_equal'] for r in results), results
    assert all(r['waits_equal'] for r in results), results
