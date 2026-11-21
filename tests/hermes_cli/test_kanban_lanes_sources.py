"""One whole-card source precedes every lanes presentation filter."""
import contextlib
import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_lanes as base

conn = base.conn
configured = base.configured
person = base.person


@pytest.fixture
def copies(conn, tmp_path):
    with contextlib.closing(kbc.connect(tmp_path / 'fleet.db')) as fleet:
        for db in (conn, fleet):
            columns = {row[1] for row in db.execute('PRAGMA table_info(tasks)')}
            if 'sync_version' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN sync_version INTEGER DEFAULT 0')
            db.execute('CREATE TABLE IF NOT EXISTS kanban_sync_outbox '
                       '(row_uid TEXT,table_name TEXT,op TEXT,state TEXT,fields TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS kanban_sync_tombstones '
                       '(table_name TEXT,row_uid TEXT,deleted_at INTEGER)')
        yield conn, fleet


def card(db, tid, status, version=1):
    db.execute('INSERT INTO tasks(id,title,status,tenant,created_at,sync_version) VALUES(?,?,?,?,?,?)',
               (tid, tid, status, 'babybear', 100, version))


@pytest.mark.parametrize('live_version,fleet_version,pending,expected', [
    (1, 2, False, 'ready'), (2, 1, False, 'running'), (2, 2, False, 'running'),
    (1, 2, True, 'running'), (2, 1, True, 'running'),
])
def test_pending_local_state_wins_else_newer_revision(copies, configured, live_version, fleet_version, pending, expected):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'running', live_version)
    card(fleet, 'c', 'ready', fleet_version)
    if pending:
        live.execute('INSERT INTO kanban_sync_outbox VALUES(?,?,?,?,?)',
                     ('c', 'tasks', 'upsert', 'pending', json.dumps({'status': 'running'})))
    chosen = Sources(live, fleet)
    assert chosen.row('c')['status'] == expected
    assert chosen.connection('c') is (live if expected == 'running' else fleet)


def test_archive_filter_tombstone_and_foreign_only(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'archived', 'archived', 2)
    card(fleet, 'archived', 'running', 1)
    card(fleet, 'deleted', 'ready')
    card(fleet, 'foreign', 'ready')
    live.execute("INSERT INTO kanban_sync_tombstones VALUES('tasks','deleted',100)")
    chosen = Sources(live, fleet)
    assert [row['id'] for row in chosen.rows] == ['foreign']
    assert chosen.row('deleted') is None
    assert chosen.row('archived')['status'] == 'archived'


def test_child_source_owns_empty_edges_parent_status_and_history(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'child', 'todo', 2)
    card(fleet, 'child', 'todo', 1)
    card(fleet, 'parent', 'ready')
    fleet.execute("INSERT INTO task_links(parent_id,child_id) VALUES('parent','child')")
    chosen = Sources(live, fleet)
    assert chosen.parents('child') == []
    live.execute("INSERT INTO task_links(parent_id,child_id) VALUES('parent','child')")
    live.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('child','commented','{}',100)")
    chosen = Sources(live, fleet)
    assert chosen.parents('child') == [('parent', 'ready')]
    assert len(chosen.events('child')) == 1


@pytest.mark.parametrize('table,uid,fields', [
    ('task_events', 'event-uid', {'task_id': 'c'}),
    ('task_links', 'parent>c', {}),
])
def test_pending_history_and_link_delete_pin_whole_card(copies, configured, table, uid, fields):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'running', 1)
    card(fleet, 'c', 'ready', 2)
    live.execute('INSERT INTO kanban_sync_outbox VALUES(?,?,?,?,?)',
                 (uid, table, 'delete', 'pending', json.dumps(fields)))
    assert Sources(live, fleet).connection('c') is live


@pytest.mark.parametrize('pending', [False, True])
def test_fleet_delete_is_final_even_before_live_apply(copies, configured, pending):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'deleted', 'running')
    fleet.execute("INSERT INTO kanban_sync_tombstones VALUES('tasks','deleted',100)")
    if pending:
        live.execute('INSERT INTO kanban_sync_outbox VALUES(?,?,?,?,?)',
                     ('deleted', 'tasks', 'upsert', 'pending', '{}'))
    chosen = Sources(live, fleet)
    assert chosen.rows == []
    assert chosen.row('deleted') is None


@pytest.mark.parametrize('raw', ['broken', '[]', 'null', '{"task_id": 5}'])
def test_malformed_task_fields_still_pin_by_task_uid(copies, configured, raw):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'running', 1)
    card(fleet, 'c', 'ready', 2)
    live.execute('INSERT INTO kanban_sync_outbox VALUES(?,?,?,?,?)',
                 ('c', 'tasks', 'upsert', 'pending', raw))
    assert Sources(live, fleet).connection('c') is live


def test_rejected_and_unrelated_table_do_not_pin(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'running', 1)
    card(fleet, 'c', 'ready', 2)
    for state, table in [('rejected', 'tasks'), ('pending', 'other')]:
        live.execute('INSERT INTO kanban_sync_outbox VALUES(?,?,?,?,?)',
                     ('c', table, 'upsert', state, '{"task_id":"c"}'))
    assert Sources(live, fleet).connection('c') is fleet


def test_foreign_child_uses_selected_local_parent_and_complete_edges(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'parent', 'done', 2)
    card(fleet, 'parent', 'ready', 1)
    card(fleet, 'child', 'todo')
    fleet.execute("INSERT INTO task_links(parent_id,child_id) VALUES('parent','child')")
    chosen = Sources(live, fleet)
    assert chosen.parents('child') == []
    assert chosen.row('parent')['status'] == 'done'


def test_selected_history_never_mixes_integer_event_ids(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'scheduled', 1)
    card(fleet, 'c', 'scheduled', 2)
    for db, kind in [(live, 'created'), (fleet, 'scheduled')]:
        db.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('c',?,'{}',100)", (kind,))
    selected = Sources(live, fleet)
    assert [e['kind'] for e in selected.events('c')] == ['scheduled']

    # A different database with equal integer event ids MUST change PR context identity.
    before = selected.identity('c')
    live.execute('UPDATE tasks SET sync_version=3 WHERE id=?', ('c',))
    assert Sources(live, fleet).identity('c') != before


def test_source_selection_only_in_primary_mode(copies, configured, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_lanes_sources import presentation
    live, fleet = copies
    card(live, 'c', 'running', 2)
    card(fleet, 'c', 'ready', 1)
    for mode in ['off', 'shadow', None, 'primary']:
        monkeypatch.setattr(sync, 'board_mode', lambda db: mode)
        evidence = presentation(live, fleet)
        assert evidence.rows[0]['status'] == ('running' if mode == 'primary' else 'ready')
    assert presentation(live, None).rows[0]['status'] == 'running'


def test_terminal_bodies_are_not_bulk_loaded(copies, configured):
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'running')
    for i in range(100):
        card(fleet, f'terminal-{i}', 'done')
    queries = []
    for db in (live, fleet):
        db.set_trace_callback(queries.append)
    try:
        chosen = Sources(live, fleet)
        assert [r['id'] for r in chosen.rows] == ['c']
    finally:
        for db in (live, fleet):
            db.set_trace_callback(None)
    full = [q for q in queries if q.startswith('SELECT * FROM tasks')]
    assert full and all('WHERE id IN' in q for q in full), full


def test_flow_held_predicate_reads_selected_source(copies, configured):
    from hermes_cli import kanban_lanes_flow as flow
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'triage', 2)
    card(fleet, 'c', 'ready', 1)
    live.execute("UPDATE tasks SET block_kind='lane_gate' WHERE id='c'")
    chosen = Sources(live, fleet)
    result = flow._card_from_row(fleet, chosen.row('c'), 200, evidence=chosen)
    assert result['held'] is True


def test_usage_counts_chosen_cards_but_keeps_canonical_run_accounting(copies, configured):
    from hermes_cli import kanban_lanes as lanes
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'local', 'running', 2)
    card(fleet, 'local', 'ready', 1)
    card(fleet, 'foreign', 'ready')
    card(live, 'finished', 'done', 2)
    card(fleet, 'finished', 'running', 1)
    for db in (live, fleet):
        db.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at) VALUES('local','done',100,150)")
        db.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('local','worker_result',?,150)",
                   (json.dumps({'usage': {'cost_usd': 2.5, 'input_tokens': 100, 'output_tokens': 10}}),))
    # Existing canonical usage is the oracle for every non-open metric.
    pol = lanes.policy()
    assert pol is not None
    canonical = lanes.usage(fleet, pol, window_s=3600, now=200)
    chosen = Sources(live, fleet)
    actual = lanes.usage(fleet, pol, window_s=3600, now=200, evidence=chosen)
    assert actual['babybear']['open'] == 2
    assert actual['babybear']['running'] == 1
    assert actual['babybear']['ready'] == 1
    for name in canonical:
        for key in ['done', 'cost_usd', 'tokens', 'runs', 'run_seconds', 'usage_runs', 'actual']:
            assert actual[name][key] == canonical[name][key]
    assert actual['babybear']['run_seconds'] == 50
    assert actual['babybear']['cost_usd'] == 2.5
    assert actual['babybear']['tokens'] == 110


def test_unresolved_deleted_attachment_does_not_hide_unrelated_owner_state(copies, configured, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_lanes_sources import presentation
    live, fleet = copies
    card(live, 'c', 'running', 2)
    card(fleet, 'c', 'ready', 1)
    live.execute('ALTER TABLE task_attachments ADD COLUMN uid TEXT')
    live.execute('INSERT INTO kanban_sync_outbox VALUES(?,?,?,?,?)',
                 ('deleted-uid', 'task_attachments', 'delete', 'pending', '{}'))
    monkeypatch.setattr(sync, 'board_mode', lambda db: 'primary')
    assert presentation(live, fleet).rows[0]['status'] == 'running'


def test_absent_parent_does_not_trust_stale_mirror(copies, configured, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'child', 'todo')
    live.execute('CREATE TABLE kanban_sync_remote_parents(parent_id TEXT,status TEXT,refreshed_at INTEGER)')
    live.execute("INSERT INTO task_links(parent_id,child_id) VALUES('absent','child')")
    live.execute("INSERT INTO kanban_sync_remote_parents VALUES('absent','done',100)")
    monkeypatch.setattr(sync, 'has_remote_parents', lambda db: db is live)
    assert Sources(live, fleet).parents('child') == [('absent', 'missing')]


def test_local_card_daily_limit_uses_canonical_usage(copies, configured):
    from hermes_cli import kanban_lanes_flow as flow, kanban_lanes as lanes
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'held', 'triage', 2)
    live.execute("UPDATE tasks SET block_kind='lane_gate' WHERE id='held'")
    live.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('held','lane_gated',?,100)",
                 (json.dumps({'reason': 'limit:cards_per_day'}),))
    card(fleet, 'foreign', 'running')
    fleet.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES('foreign','lane_admitted',?,100)",
                  (json.dumps({'lane': 'babybear'}),))
    pol = lanes.parse({'lane_default': 'babybear', 'lanes': {'babybear': {'cards_per_day': 1}}})
    sources = Sources(live, fleet)
    result = flow._card_from_row(fleet, sources.row('held'), 200, pol=pol, evidence=sources)
    assert result['gate_reason'] == 'limit:cards_per_day'


def test_cli_and_flow_share_one_view_without_reopening(copies, configured, monkeypatch):
    from hermes_cli import kanban_lanes_cli as cli, kanban_lanes_flow as flow
    from hermes_cli.kanban_lanes_sources import Sources
    live, fleet = copies
    card(live, 'c', 'running', 2)
    card(fleet, 'c', 'ready', 1)
    evidence = Sources(live, fleet)
    monkeypatch.setattr(cli, '_fleet', lambda *a, **k: pytest.fail('borrowed view reopened replica'))
    class EmptyReader:
        def prefetch(self, urls):
            pass
        def get(self, url):
            return {}
    strip = cli.snapshot(live, hours=6, evidence=evidence)
    panel = flow.snapshot(fleet, window='6h', evidence=evidence, reader=EmptyReader())
    assert strip['lanes']['babybear']['running'] == 1
    assert strip['lanes']['babybear']['open'] == len(panel['cards']) == 1
    assert panel['cards'][0]['step'] == 'running'


def test_read_view_releases_only_its_transactions_and_replica(copies, configured, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_lanes_cli as cli
    from hermes_cli.kanban_lanes_sources import read_view
    live, fleet = copies
    card(live, 'local', 'ready')
    monkeypatch.setattr(cli, '_fleet', lambda *a, **k: fleet)
    with read_view(live) as evidence:
        assert evidence.conn is fleet
        assert live.in_transaction and fleet.in_transaction
    assert not live.in_transaction
    with pytest.raises(sqlite3.ProgrammingError):
        fleet.execute('SELECT 1')
    monkeypatch.setattr(cli, '_fleet', lambda *a, **k: None)
    live.execute('BEGIN')
    card(live, 'caller-uncommitted', 'ready')
    with read_view(live) as evidence:
        assert len(evidence.rows) == 2
    assert live.in_transaction
    live.rollback()
    assert live.execute("SELECT 1 FROM tasks WHERE id='caller-uncommitted'").fetchone() is None

