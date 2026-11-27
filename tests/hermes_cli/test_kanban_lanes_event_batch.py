"""Lanes histories use bounded source-owned reads, not one query per card."""
import contextlib
import json
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
                           (str(i), 'created', '{"pr":null,"source":"' + ('local' if db is live else 'fleet') + '"}', stamp))
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



def test_irrelevant_payloads_are_not_decoded(conn, monkeypatch):
    from hermes_cli import kanban_lanes_flow as flow

    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('noise','noise','ready',1)")
    conn.executemany(
        'INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)',
        [('noise', kind, '{"unrelated": true}', 1)
         for kind in ('heartbeat', 'external_watch') for _ in range(1000)])
    decoded = []
    original = flow._payload

    def counted(event):
        decoded.append(event)
        return original(event)

    monkeypatch.setattr(flow, '_payload', counted)
    assert Evidence(conn).events('noise') == []
    assert decoded == []


@pytest.mark.parametrize('key', ['published_pr', 'reviewed_pr', 'pr', 'flow_pr', 'head', 'head_sha', 'headRefOid'])
@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('escaped', [False, True])
def test_pr_and_head_evidence_survives_any_event_kind(conn, key, nested, escaped):
    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('c','c','ready',1)")
    value = {key: 'value'}
    payload = json.dumps({'metadata': value} if nested else value)
    if escaped:
        encoded = ''.join(f'\\u{ord(c):04x}' for c in key)
        payload = payload.replace('"' + key + '"', '"' + encoded + '"')
    conn.execute('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)',
                 ('c', 'custom_observation', payload, 1))
    event = Evidence(conn).events('c')[0]
    assert event['kind'] == 'custom_observation'
    assert event['payload'] == ({'metadata': value} if nested else value)


def test_flow_history_compacts_only_unconsumed_occurrences(conn):
    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('c','c','ready',1)")
    histories = [('created', '{"creator_task_id":"first"}'),
                 ('created', '{"creator_task_id":"second"}'),
                 ('dispatch_skipped', '{"reason":"llm_paced"}'),
                 ('claimed', '{}'), ('dispatch_skipped', '{"reason":"other"}'),
                 ('scheduled', '{"awaiting":{"kind":"ci"}}'),
                 ('scheduled', '{"metadata":{"flow_pr":{"merged":true}}}'),
                 ('scheduled', '{}')]
    for kind, payload in histories:
        conn.execute('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES(?,?,?,?)',
                     ('c', kind, payload, 1))
    events = Evidence(conn).events('c')
    assert [(e['kind'], e['payload']) for e in events] == [
        (kind, json.loads(payload)) for index, (kind, payload) in enumerate(histories)
        if index not in (1, 2, 5)]


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
                       [(i, str(i), 'created', '{}') for i in range(805)])
        evidence = Evidence(db)
        queries = []
        db.set_trace_callback(queries.append)
        for row in evidence.rows:
            assert evidence.events(row['id']) == [
                {'id': int(row['id']), 'kind': 'created', 'payload': {}}]
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
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('0','created','{}',1)")

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
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('parent','completed','{}',1)")
    assert evidence.events('parent')[0]['kind'] == 'completed'

