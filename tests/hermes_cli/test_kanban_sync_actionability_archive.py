"""Archive withdraws stale diagnostics, never delivers or completes them. [daveey]"""
import json

import pytest

from hermes_cli import kanban_sync_actionability_drift as audit

pytest_plugins = ['tests.hermes_cli.test_kanban_sync_actionability_terminal']


@pytest.fixture(params=[False, True], ids=['withdrawn', 'done-then-archived'])
def archived(completed, request):
    live, shadow, op = completed
    for conn in (live, shadow):
        if not request.param:
            conn.execute("DELETE FROM task_events WHERE kind='completed'")
            conn.execute("UPDATE tasks SET completed_at=NULL")
            conn.execute("UPDATE task_runs SET status='retained',outcome='retained'")
        conn.execute("UPDATE tasks SET status='archived' WHERE id='t_card'")
        payload = {'source_status': 'done' if request.param else 'triage',
                   '_recovery_history_v1': {'identity': 'archive-identity',
                                            'predecessors': ['completion'] if request.param else []}}
        conn.execute("INSERT INTO task_events VALUES (3,'archive','t_card','archived',130,?,NULL,NULL)",
                     (json.dumps(payload),))
    return live, shadow, op


def test_archive_is_read_only_supersession_not_success_or_delivery(archived):
    from hermes_cli.kanban_rejected_retain_retirement import retire
    from hermes_cli.kanban_sync_client import diff

    live, shadow, op = archived
    before = [list(c.iterdump()) for c in (live, shadow)]
    assert audit._source(live, 't_card') is None
    assert audit.audited_rejection(live, shadow, op)
    report = diff(live, shadow, 'macbook')
    assert report['clean'], report
    assert report['pending'] == 0
    assert report['rejected_ops'] == 1
    assert report['audited_retain_rejections'] == [op['op_id']]
    assert retire(live, None, 'default', op)['action'] == 'retained'
    assert [list(c.iterdump()) for c in (live, shadow)] == before


@pytest.mark.parametrize('copies', ['live', 'shadow', 'both'])
@pytest.mark.parametrize('sql', [
    "DELETE FROM task_events WHERE kind='archived'",
    "UPDATE task_events SET deleted_at=1 WHERE kind='archived'",
    "UPDATE task_events SET payload='{' WHERE kind='archived'",
    "UPDATE task_events SET payload='[]' WHERE kind='archived'",
    "UPDATE task_events SET payload='{}' WHERE kind='archived'",
    "UPDATE task_events SET created_at=123 WHERE kind='archived'",
    "UPDATE tasks SET status='ready' WHERE id='t_card'",
    "UPDATE tasks SET current_run_id=9 WHERE id='t_card'",
    "UPDATE tasks SET claim_lock='worker' WHERE id='t_card'",
    "UPDATE tasks SET claim_expires=999 WHERE id='t_card'",
    "UPDATE tasks SET worker_pid=1 WHERE id='t_card'",
    "UPDATE tasks SET containment_episode='held' WHERE id='t_card'",
    "UPDATE tasks SET deleted_at=1 WHERE id='t_card'",
    "UPDATE task_runs SET ended_at=NULL",
    "UPDATE task_runs SET status='running'",
    "UPDATE task_runs SET outcome=NULL",
    "UPDATE task_runs SET started_at=-1",
    "UPDATE task_runs SET ended_at=131",
    "UPDATE task_events SET run_id=99 WHERE kind='archived'",
    "UPDATE task_events SET payload='[]' WHERE uid='source'",
    "UPDATE task_events SET deleted_at=1 WHERE uid='source'",
    "INSERT INTO task_events VALUES (4,'reopen','t_card','reopened',131,'{}',NULL,NULL)",
    "INSERT INTO task_events VALUES (4,'hold','t_card','contained',131,'{}',NULL,NULL)",
    "INSERT INTO task_events VALUES (4,'bad','t_card','claimed',129,'{',NULL,NULL)",
    "INSERT INTO task_events VALUES (4,'custom','t_card','custom',131,'{\"status\":\"ready\"}',NULL,NULL)",
])
def test_archive_unproven_evidence_stays_dirty(archived, copies, sql):
    live, shadow, op = archived
    for name, conn in [('live', live), ('shadow', shadow)]:
        if copies in ('both', name):
            conn.execute(sql)
    assert not audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('sql', [
    "UPDATE task_events SET uid='other' WHERE kind='archived'",
    "UPDATE task_events SET payload='{\"source_status\":\"ready\"}' WHERE kind='archived'",
    "UPDATE task_events SET payload='{\"different\":1}' WHERE uid='source'",
    "UPDATE task_runs SET uid='other'",
    "DELETE FROM task_runs",
    "UPDATE tasks SET assignee='other' WHERE id='t_card'",
])
def test_archive_portable_inventory_must_match(archived, sql):
    live, shadow, op = archived
    shadow.execute(sql)
    assert not audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('column,value', [
    ('request_json', '{}'), ('request_sha256', 'bad'), ('delivery_state', 'confirmed'),
    ('canonical_receipt_json', '{}'), ('contract_key', 'other'),
])
def test_archive_never_bypasses_original_command_custody(archived, column, value):
    live, shadow, op = archived
    live.execute(f'UPDATE kanban_actionability_commands SET {column}=?', (value,))
    assert not audit.audited_rejection(live, shadow, op)


def test_archive_run_and_event_ids_are_local(archived):
    live, shadow, op = archived
    shadow.execute('UPDATE task_runs SET id=99')
    shadow.execute("UPDATE task_events SET run_id=99 WHERE kind='completed'")
    assert audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('change', [
    'missing-meta', 'empty-meta', 'bad-identity', 'bad-predecessors', 'missing-reference',
    'duplicate-reference', 'self-cycle', 'fork', 'later-history', 'different-history',
])
def test_archive_causal_provenance_fails_closed(archived, change):
    live, shadow, op = archived
    for conn in (live, shadow):
        row = conn.execute("SELECT payload FROM task_events WHERE kind='archived'").fetchone()
        payload = json.loads(row[0])
        meta = payload['_recovery_history_v1']
        mutations = {
            'missing-meta': lambda: payload.pop('_recovery_history_v1'),
            'empty-meta': lambda: payload.update(_recovery_history_v1={}),
            'bad-identity': lambda: meta.update(identity=[]),
            'bad-predecessors': lambda: meta.update(predecessors='completion'),
            'missing-reference': lambda: meta.update(predecessors=['absent']),
            'duplicate-reference': lambda: meta.update(predecessors=['completion', 'completion']),
            'self-cycle': lambda: meta.update(predecessors=['archive-identity']),
        }
        if change in mutations:
            mutations[change]()
        else:
            stamp = {'identity': 'prior-identity', 'predecessors': []}
            event = {'_recovery_history_v1': stamp}
            if change == 'different-history' and conn is shadow:
                event['extra'] = True
            conn.execute("INSERT INTO task_events VALUES (4,'prior','t_card','reclaimed',?, ?,NULL,NULL)",
                         (131 if change == 'later-history' else 128, json.dumps(event)))
            if change != 'fork':
                meta['predecessors'].append('prior-identity')
        conn.execute("UPDATE task_events SET payload=? WHERE kind='archived'", (json.dumps(payload),))
    assert not audit.audited_rejection(live, shadow, op)


def test_active_done_archive_transitions_keep_rejection_custody(completed):
    live, shadow, op = completed
    custody = list(live.execute('SELECT * FROM kanban_actionability_commands'))
    outbox = list(live.execute('SELECT * FROM kanban_sync_outbox'))
    for conn in (live, shadow):
        conn.execute("UPDATE tasks SET status='todo',completed_at=NULL WHERE id='t_card'")
        conn.execute("UPDATE tasks SET status='running' WHERE id='t_parent'")
        conn.execute("DELETE FROM task_events WHERE kind='completed'")
    assert not audit.audited_rejection(live, shadow, op)
    for conn in (live, shadow):
        conn.execute("UPDATE tasks SET status='done',completed_at=125 WHERE id='t_card'")
        conn.execute("INSERT INTO task_events VALUES (2,'completion','t_card','completed',125,'{}',NULL,9)")
    assert audit.audited_rejection(live, shadow, op)
    for conn in (live, shadow):
        conn.execute("UPDATE tasks SET status='archived' WHERE id='t_card'")
        payload = {'source_status': 'done', '_recovery_history_v1': {
            'identity': 'archive', 'predecessors': ['completion']}}
        conn.execute("INSERT INTO task_events VALUES (3,'archive','t_card','archived',130,?,NULL,NULL)",
                     (json.dumps(payload),))
    assert audit.audited_rejection(live, shadow, op)
    assert list(live.execute('SELECT * FROM kanban_actionability_commands')) == custody
    assert list(live.execute('SELECT * FROM kanban_sync_outbox')) == outbox


def test_archive_all_closed_runs_not_just_latest_must_match(archived):
    live, shadow, op = archived
    for conn in (live, shadow):
        conn.execute("INSERT INTO task_runs VALUES (10,'earlier','t_card','continued','continued',120,121)")
    assert audit.audited_rejection(live, shadow, op)
    shadow.execute("UPDATE task_runs SET ended_at=122 WHERE uid='earlier'")
    assert not audit.audited_rejection(live, shadow, op)


def test_archive_allows_no_runs_and_post_archive_bookkeeping(archived):
    live, shadow, op = archived
    for conn in (live, shadow):
        conn.execute('DELETE FROM task_runs')
        conn.execute("UPDATE task_events SET run_id=NULL")
        conn.execute("INSERT INTO task_events VALUES (4,'bookkeeping','t_card','archive_worker_termination',131,'{}',NULL,NULL)")
    assert audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('offset,expected', [(1, True), (0, False), (-1, False)])
def test_archive_same_second_requires_portable_strict_order(archived, offset, expected):
    import uuid

    live, shadow, op = archived
    def uid(ms):
        return str(uuid.UUID(int=(ms << 80) | (7 << 76) | (2 << 62)))
    for conn in (live, shadow):
        conn.execute("UPDATE task_events SET uid=?,created_at=130 WHERE kind='archived'", (uid(130500),))
        conn.execute("INSERT INTO task_events VALUES (4,?,'t_card','custom',130,?,NULL,NULL)",
                     (uid(130500-offset), json.dumps({'status': 'triage'})))
    assert audit.audited_rejection(live, shadow, op) is expected
