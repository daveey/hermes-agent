"""Superseded diagnostic rejection remains immutable audit, not drift. [daveey]"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_actionability_transport as transport
from hermes_cli import kanban_sync_retain_drift as drift


@pytest.fixture
def stores():
    live, shadow = (sqlite3.connect(':memory:') for _ in range(2))
    for conn in (live, shadow):
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE tasks(id TEXT, current_run_id INTEGER, status TEXT, assignee TEXT,
                containment_episode TEXT, deleted_at INTEGER, sync_version INTEGER, created_on TEXT);
            CREATE TABLE task_runs(id INTEGER, uid TEXT, task_id TEXT);
            CREATE TABLE task_events(id INTEGER, uid TEXT, task_id TEXT, kind TEXT,
                created_at INTEGER, payload TEXT, deleted_at INTEGER);
            CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
            CREATE TABLE task_comments(task_id TEXT, uid TEXT);
            CREATE TABLE task_chat_links(task_id TEXT, uid TEXT, chat_gateway TEXT);
            INSERT INTO tasks VALUES ('t_card',NULL,'todo','default',NULL,NULL,1,'macbook');
            INSERT INTO tasks VALUES ('t_parent',NULL,'scheduled','default',NULL,NULL,1,'macbook');
            INSERT INTO task_events VALUES (1,'source','t_card','created',123,'{}',NULL);
            INSERT INTO task_links VALUES ('t_parent','t_card');
        ''')
    transport.ensure_schema(live)
    live.execute('CREATE TABLE kanban_sync_outbox(op_id TEXT, row_uid TEXT, table_name TEXT, '
                 'op TEXT, state TEXT, last_error TEXT, fields TEXT)')
    facts = {'status': 'todo', 'assignee': 'default', 'current_run_uid': None, 'root_episode': None,
             'source_uids': [{'uid': 'source', 'version': 1}],
             'dependencies': [{'id': 't_parent', 'status': 'running'}]}
    payload = {'facts': facts, 'owner': {'kind': 'assignee', 'refs': ['default']},
               'blocker': {'prerequisite': 'parent', 'trigger': 'parent'}, 'action': 'satisfy_dependency'}
    wire = transport.envelope('t_card', payload)
    text = transport.canonical(wire)
    live.execute('INSERT INTO kanban_actionability_commands '
                 '(op_id,task_id,contract_key,phase,request_json,request_sha256,delivery_state) '
                 "VALUES (?,?,?,?,?,?,'rejected')", (wire['client_op_id'], 't_card',
                 wire['payload']['contract_key'], 'contract', text, transport.digest(text)))
    op = dict(op_id=wire['client_op_id'], row_uid='t_card', table_name='tasks', op=transport.KIND,
              state='rejected', last_error='actionability_source_changed', fields=text)
    live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                 tuple(op[k] for k in ('op_id', 'row_uid', 'table_name', 'op', 'state', 'last_error', 'fields')))
    yield live, shadow, op
    live.close()
    shadow.close()


def test_exact_superseded_rejection_is_read_only(stores):
    from hermes_cli.kanban_sync_client import diff

    live, shadow, op = stores
    before = [list(c.iterdump()) for c in (live, shadow)]
    assert drift.audited_rejection(live, shadow, op)
    report = diff(live, shadow, 'macbook')
    assert report['clean'], report
    assert report['rejected_ops'] == 1
    assert report['audited_retain_rejections'] == [op['op_id']]
    assert [list(c.iterdump()) for c in (live, shadow)] == before
    shadow.execute("UPDATE tasks SET assignee='other' WHERE id='t_card'")
    assert not diff(live, shadow, 'macbook')['clean']


@pytest.mark.parametrize('column,value', [
    ('op_id', 'other'), ('task_id', 'other'), ('contract_key', 'other'), ('phase', 'escalation'),
    ('request_json', '{}'), ('request_sha256', 'bad'), ('delivery_state', 'unknown'),
    ('canonical_receipt_json', '{}'),
])
def test_each_custody_mismatch_stays_dirty(stores, column, value):
    live, shadow, op = stores
    live.execute(f'UPDATE kanban_actionability_commands SET {column}=?', (value,))
    assert not drift.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('change', [
    'no_custody', 'table', 'operation', 'state', 'reason', 'bad_json', 'wire_id', 'wire_task',
    'wire_op', 'envelope', 'missing_live', 'missing_shadow', 'live_unchanged', 'shadow_unchanged',
    'both_unchanged', 'disagree', 'missing_parent', 'missing_source', 'bad_source_uid',
    'missing_run', 'legacy_schema', 'bad_facts', 'optional_kind', 'optional_request',
])
def test_incomplete_supersession_or_custody_stays_dirty(stores, change):
    live, shadow, op = stores
    if change == 'no_custody':
        live.execute('DROP TABLE kanban_actionability_commands')
    elif change in ('table', 'operation', 'state', 'reason', 'bad_json'):
        key, value = {'table': ('table_name', 'task_events'), 'operation': ('op', 'retain'),
                      'state': ('state', 'pending'), 'reason': ('last_error', 'forbidden'),
                      'bad_json': ('fields', '{')}[change]
        op[key] = value
    elif change.startswith('wire_') or change in ('envelope', 'bad_facts'):
        wire = json.loads(op['fields'])
        if change.startswith('wire_'):
            wire[{'wire_id': 'client_op_id', 'wire_task': 'task', 'wire_op': 'op'}[change]] = 'other'
        elif change == 'envelope':
            wire['receipt_uid'] = 'other'
        else:
            wire['payload']['facts']['source_uids'] = None
        op['fields'] = transport.canonical(wire)
        live.execute('UPDATE kanban_actionability_commands SET request_json=?,request_sha256=?',
                     (op['fields'], transport.digest(op['fields'])))
    elif change.startswith('optional_'):
        column = 'operation_kind' if change == 'optional_kind' else 'request_uid'
        live.execute(f'ALTER TABLE kanban_actionability_commands ADD COLUMN {column} TEXT')
        live.execute(f"UPDATE kanban_actionability_commands SET {column}='other'")
    else:
        targets = (live, shadow) if change == 'both_unchanged' else (shadow,) if change in (
            'shadow_unchanged', 'missing_shadow') else (live,)
        sql = {'missing_live': "DELETE FROM tasks WHERE id='t_card'",
               'missing_shadow': "DELETE FROM tasks WHERE id='t_card'",
               'live_unchanged': "UPDATE tasks SET status='running' WHERE id='t_parent'",
               'shadow_unchanged': "UPDATE tasks SET status='running' WHERE id='t_parent'",
               'both_unchanged': "UPDATE tasks SET status='running' WHERE id='t_parent'",
               'disagree': "UPDATE tasks SET status='ready' WHERE id='t_parent'",
               'missing_parent': "DELETE FROM tasks WHERE id='t_parent'",
               'missing_source': 'DELETE FROM task_events',
               'bad_source_uid': 'UPDATE task_events SET uid=NULL',
               'missing_run': "UPDATE tasks SET current_run_id=9 WHERE id='t_card'",
               'legacy_schema': 'DROP TABLE task_events'}[change]
        for conn in targets:
            conn.execute(sql)
    assert not drift.audited_rejection(live, shadow, op)


def test_retain_retirement_never_accepts_diagnostic(stores):
    from hermes_cli.kanban_rejected_retain_retirement import retire

    live, _, op = stores
    before = list(live.iterdump())
    assert retire(live, None, 'default', op)['action'] == 'retained'
    assert list(live.iterdump()) == before


def _refreeze(live, op, facts):
    old = json.loads(op['fields'])
    payload = old['payload']
    payload.pop('contract_key')
    payload['facts'] = facts
    wire = transport.envelope(old['task'], payload)
    text = transport.canonical(wire)
    live.execute('UPDATE kanban_actionability_commands SET op_id=?,contract_key=?,request_json=?,request_sha256=?',
                 (wire['client_op_id'], wire['payload']['contract_key'], text, transport.digest(text)))
    op.update(op_id=wire['client_op_id'], fields=text)


@pytest.mark.parametrize('key,value', [
    ('status', None), ('assignee', []), ('current_run_uid', 1), ('root_episode', {}),
    ('source_uids', None), ('source_uids', []), ('source_uids', ['source']),
    ('source_uids', [{'uid': 'source'}]), ('source_uids', [{'uid': '', 'version': 1}]),
    ('source_uids', [{'uid': 1, 'version': 1}]), ('source_uids', [{'uid': 'source', 'version': True}]),
    ('source_uids', [{'uid': 'source', 'version': 0}]), ('dependencies', None),
    ('dependencies', ['parent']), ('dependencies', {}), ('dependencies', [{'id': 't_parent'}]),
    ('source_uids', [{'uid': 'source', 'version': 1, 'extra': 'unknown'}]),
    ('dependencies', [{'id': '', 'status': 'running'}]),
    ('dependencies', [{'id': 't_parent', 'status': None}]),
    ('dependencies', [{'id': 't_parent', 'status': 'running'}] * 2),
], ids=lambda value: str(value)[:60])
def test_malformed_frozen_facts_with_matching_digest_stay_dirty(stores, key, value):
    live, shadow, op = stores
    facts = json.loads(op['fields'])['payload']['facts']
    facts[key] = value
    _refreeze(live, op, facts)
    assert not drift.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('facts', [None, [], {'extra': 1}], ids=['null', 'list', 'incomplete'])
def test_invalid_facts_container_stays_dirty(stores, facts):
    live, shadow, op = stores
    _refreeze(live, op, facts)
    assert not drift.audited_rejection(live, shadow, op)


def test_extra_frozen_fact_is_not_understood(stores):
    live, shadow, op = stores
    facts = json.loads(op['fields'])['payload']['facts']
    facts['unknown'] = 'value'
    _refreeze(live, op, facts)
    assert not drift.audited_rejection(live, shadow, op)


def test_version_only_difference_cannot_prove_supersession(stores):
    live, shadow, op = stores
    facts = json.loads(op['fields'])['payload']['facts']
    facts['dependencies'][0]['status'] = 'scheduled'
    facts['source_uids'][0]['version'] = 2
    _refreeze(live, op, facts)
    assert not drift.audited_rejection(live, shadow, op)


def test_parent_fact_bound_is_independent_of_custody(stores):
    live, shadow, op = stores
    facts = json.loads(op['fields'])['payload']['facts']
    facts['dependencies'] = [{'id': f'p{i:03}', 'status': 'done'} for i in range(129)]
    _refreeze(live, op, facts)
    assert not drift.audited_rejection(live, shadow, op)


def test_resolved_run_without_portable_identity_stays_dirty(stores):
    live, shadow, op = stores
    for conn in (live, shadow):
        conn.execute("INSERT INTO task_runs VALUES (9,'','t_card')")
        conn.execute("UPDATE tasks SET current_run_id=9 WHERE id='t_card'")
    assert not drift.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET deleted_at=1 WHERE id='t_card'",
    "UPDATE task_events SET deleted_at=1 WHERE uid='source'",
    "UPDATE tasks SET current_run_id=9 WHERE id='t_card'",
])
def test_agreeing_invalid_current_sources_stay_dirty(stores, sql):
    live, shadow, op = stores
    for conn in (live, shadow):
        conn.execute(sql)
    assert not drift.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('copies', ['both', 'live', 'shadow'])
def test_missing_frozen_event_with_other_source_is_not_supersession(stores, copies):
    live, shadow, op = stores
    for name, conn in [('live', live), ('shadow', shadow)]:
        conn.execute("INSERT INTO task_events VALUES (2,'new-source','t_card','linked',124,'{}',NULL)")
        if copies in ('both', name):
            conn.execute("DELETE FROM task_events WHERE uid='source'")
    assert not drift.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('frozen_status', ['running', 'ready', 'scheduled', 'done'])
@pytest.mark.parametrize('current_status', ['running', 'ready', 'scheduled', 'done'])
@pytest.mark.parametrize('copies', ['both', 'live', 'shadow'])
def test_dependency_status_product_requires_changed_agreeing_copies(stores, frozen_status, current_status, copies):
    live, shadow, op = stores
    facts = json.loads(op['fields'])['payload']['facts']
    facts['dependencies'][0]['status'] = frozen_status
    _refreeze(live, op, facts)
    for name, conn in [('live', live), ('shadow', shadow)]:
        status = current_status if copies in ('both', name) else frozen_status
        conn.execute("UPDATE tasks SET status=? WHERE id='t_parent'", (status,))
    assert drift.audited_rejection(live, shadow, op) == (copies == 'both' and current_status != frozen_status)


@pytest.mark.parametrize('change', ['run', 'source', 'episode', 'parent_set', 'held_parent'])
def test_positive_other_portable_source_changes(stores, change):
    live, shadow, op = stores
    for conn in (live, shadow):
        conn.execute("UPDATE tasks SET status='running' WHERE id='t_parent'")
        statements = {
            'run': ["INSERT INTO task_runs VALUES (9,'new-run','t_card')",
                    "UPDATE tasks SET current_run_id=9 WHERE id='t_card'"],
            'source': ["INSERT INTO task_events VALUES (2,'new-source','t_card','linked',124,'{}',NULL)"],
            'episode': ["UPDATE tasks SET containment_episode='source' WHERE id='t_card'"],
            'parent_set': ['DELETE FROM task_links'],
            'held_parent': ["UPDATE tasks SET status='done',containment_episode='held' WHERE id='t_parent'"],
        }
        for sql in statements[change]:
            conn.execute(sql)
    assert drift.audited_rejection(live, shadow, op)
