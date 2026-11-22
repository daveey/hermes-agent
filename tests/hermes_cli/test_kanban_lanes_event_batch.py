"""Lanes histories use bounded source-owned reads, not one query per card."""
import contextlib
import sqlite3

import pytest

from hermes_cli.kanban_lanes_flow_evidence import Evidence
from tests.hermes_cli import test_kanban_lanes_sources as sources_base

conn = sources_base.conn
copies = sources_base.copies
configured = sources_base.configured


def test_selected_sources_batch_only_their_own_card_histories(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    for i in range(12):
        for db, version in ((live, 2 if i % 2 else 1), (fleet, 1 if i % 2 else 2)):
            sources_base.card(db, str(i), 'ready', version)
            for stamp in (3, 1, 2):
                db.execute('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)',
                           (str(i), 'commented', '{"source":"' + ('local' if db is live else 'fleet') + '"}', stamp))
    chosen = Sources(live, fleet)
    queries = []
    live.set_trace_callback(queries.append)
    fleet.set_trace_callback(queries.append)
    for row in chosen.rows:
        events = chosen.events(row['id'])
        assert len(events) == 3
        assert [e['id'] for e in events] == sorted(e['id'] for e in events)
        assert {e['payload']['source'] for e in events} == ({'local'} if int(row['id']) % 2 else {'fleet'})
    assert len([q for q in queries if 'FROM task_events' in q]) == 2



def test_open_card_histories_are_batched_and_cached_per_view():
    with contextlib.closing(sqlite3.connect(':memory:')) as db:
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE tasks(id TEXT, status TEXT, created_at INTEGER);
            CREATE TABLE task_events(id INTEGER, task_id TEXT, kind TEXT, payload TEXT);
        ''')
        db.executemany('INSERT INTO tasks VALUES (?, ?, ?)',
                       [(str(i), 'ready', i) for i in range(805)])
        db.executemany('INSERT INTO task_events VALUES (?, ?, ?, ?)',
                       [(i, str(i), 'commented', '{}') for i in range(805)])
        evidence = Evidence(db)
        queries = []
        db.set_trace_callback(queries.append)
        for row in evidence.rows:
            assert evidence.events(row['id']) == [
                {'id': int(row['id']), 'kind': 'commented', 'payload': {}}]
        reads = [q for q in queries if 'FROM task_events' in q]
        assert len(reads) == 3, reads
        queries.clear()
        assert evidence.events('1')[0]['id'] == 1
        assert not queries
        # A new view must observe edits; the cache never crosses requests.
        db.execute("UPDATE task_events SET payload='{\"changed\":true}' WHERE id=1")
        assert Evidence(db).events('1')[0]['payload'] == {'changed': True}


def test_failed_batch_is_not_published_and_retry_reads_fresh(conn):
    for i in range(401):
        conn.execute('INSERT INTO tasks(id,title,status,created_at) VALUES(?,?,?,?)',
                     (str(i), str(i), 'ready', i))
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('0','commented','{}',1)")

    class FailSecondBatch:
        def __init__(self):
            self.reads = 0

        def execute(self, sql, args=()):
            if 'FROM task_events' in sql:
                self.reads += 1
                if self.reads == 2:
                    raise sqlite3.OperationalError('fixture read failure')
            return conn.execute(sql, args)

    evidence = Evidence(FailSecondBatch())
    with pytest.raises(sqlite3.OperationalError, match='fixture read failure'):
        evidence.events('0')
    assert not evidence._events
    conn.execute("UPDATE task_events SET payload='{\"retry\":true}' WHERE task_id='0'")
    assert evidence.events('0')[0]['payload'] == {'retry': True}
    assert evidence.events('400') == []
    # A terminal parent is not part of open-card prefetch; read it on demand.
    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('parent','parent','done',0)")
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('parent','done','{}',1)")
    assert evidence.events('parent')[0]['kind'] == 'done'

