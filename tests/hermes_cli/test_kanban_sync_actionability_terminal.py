"""Done-task diagnostic supersession is proof, never transport delivery. [daveey]"""
import json
import uuid

import pytest

from hermes_cli import kanban_sync_actionability_drift as audit
pytest_plugins = ['tests.hermes_cli.test_kanban_sync_actionability_drift']


@pytest.fixture(name='completed')
def completed_store(stores):
    live, shadow, op = stores
    for conn in (live, shadow):
        for column in ('claim_lock TEXT', 'claim_expires INTEGER', 'worker_pid INTEGER', 'completed_at INTEGER'):
            conn.execute('ALTER TABLE tasks ADD COLUMN ' + column)
        for column in ('status TEXT', 'outcome TEXT', 'started_at INTEGER', 'ended_at INTEGER'):
            conn.execute('ALTER TABLE task_runs ADD COLUMN ' + column)
        conn.execute('ALTER TABLE task_events ADD COLUMN run_id INTEGER')
        conn.execute("INSERT INTO task_runs VALUES (9,'completed-run','t_card','done','completed',124,125)")
        conn.execute("UPDATE tasks SET status='done',completed_at=125 WHERE id='t_card'")
        conn.execute("INSERT INTO task_events VALUES (2,'completion','t_card','completed',125,'{}',NULL,9)")
    return live, shadow, op


def test_todo_to_done_is_audited_without_mutating_or_delivering(completed):
    from hermes_cli.kanban_sync_client import diff
    from hermes_cli.kanban_rejected_retain_retirement import retire

    live, shadow, op = completed
    before = [list(c.iterdump()) for c in (live, shadow)]
    assert audit._source(live, 't_card') is None  # ordinary selection stays unchanged
    assert audit.audited_rejection(live, shadow, op)
    report = diff(live, shadow, 'macbook')
    assert report['clean'], report
    assert report['rejected_ops'] == 1
    assert report['audited_retain_rejections'] == [op['op_id']]
    assert retire(live, None, 'default', op)['action'] == 'retained'
    assert [list(c.iterdump()) for c in (live, shadow)] == before


@pytest.mark.parametrize('copies', ['live', 'shadow', 'both'])
@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET status='archived' WHERE id='t_card'",
    "UPDATE tasks SET status='ready' WHERE id='t_card'",
    "UPDATE tasks SET current_run_id=9 WHERE id='t_card'",
    "UPDATE tasks SET claim_lock='worker' WHERE id='t_card'",
    "UPDATE tasks SET claim_expires=999 WHERE id='t_card'",
    "UPDATE tasks SET worker_pid=1 WHERE id='t_card'",
    "UPDATE tasks SET containment_episode='held' WHERE id='t_card'",
    "UPDATE tasks SET completed_at=126 WHERE id='t_card'",
    "DELETE FROM task_events WHERE kind='completed'",
    "UPDATE task_events SET payload='{' WHERE kind='completed'",
    "UPDATE task_events SET deleted_at=1 WHERE kind='completed'",
    "UPDATE task_events SET created_at=123 WHERE kind='completed'",
    "UPDATE task_events SET run_id=NULL WHERE kind='completed'",
    "UPDATE task_runs SET ended_at=NULL",
    "UPDATE task_runs SET outcome='continued'",
    "UPDATE task_runs SET status='running'",
    "UPDATE task_runs SET started_at=126",
    "INSERT INTO task_runs VALUES (10,'active','t_card','running',NULL,126,NULL)",
    "INSERT INTO task_events VALUES (3,'reopen','t_card','reopened',126,'{}',NULL,NULL)",
    "INSERT INTO task_events VALUES (3,'claim','t_card','claimed',126,'{}',NULL,NULL)",
    "INSERT INTO task_events VALUES (3,'status','t_card','status',126,'{',NULL,NULL)",
    "UPDATE task_events SET payload='[]' WHERE uid='source'",
    "UPDATE task_events SET deleted_at=1 WHERE uid='source'",
    "INSERT INTO task_events VALUES (3,'change','t_card','changes_requested',126,'{}',NULL,NULL)",
    "INSERT INTO task_events VALUES (3,'release','t_card','containment_released',126,'{',NULL,NULL)",
    "INSERT INTO task_events VALUES (3,'custom','t_card','custom',126,'{\"status\":\"ready\"}',NULL,NULL)",
    "INSERT INTO task_runs VALUES (10,'later','t_card','blocked','blocked',126,127)",
    "INSERT INTO task_runs VALUES (10,'bad','t_card','blocked','blocked',126,123)",
])
def test_unproven_terminal_evidence_stays_dirty(completed, copies, sql):
    live, shadow, op = completed
    for name, conn in [('live', live), ('shadow', shadow)]:
        if copies in ('both', name):
            conn.execute(sql)
    assert not audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('sql', [
    "UPDATE task_events SET uid='other' WHERE kind='completed'",
    "UPDATE task_events SET payload='{\"summary\":\"different\"}' WHERE kind='completed'",
    "UPDATE task_events SET payload='{\"different\":1}' WHERE uid='source'",
    "UPDATE task_runs SET uid='other'",
])
def test_divergent_portable_custody_stays_dirty(completed, sql):
    live, shadow, op = completed
    shadow.execute(sql)
    assert not audit.audited_rejection(live, shadow, op)


def test_local_event_and_run_ids_are_not_portable_order(completed):
    live, shadow, op = completed
    shadow.execute("UPDATE task_runs SET id=99")
    shadow.execute("UPDATE task_events SET run_id=99 WHERE kind='completed'")
    shadow.execute("UPDATE task_events SET id=100-id")
    assert audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('column,value', [
    ('request_json', '{}'), ('request_sha256', 'bad'), ('delivery_state', 'confirmed'),
    ('canonical_receipt_json', '{}'), ('contract_key', 'other'),
])
def test_terminal_branch_never_bypasses_command_custody(completed, column, value):
    live, shadow, op = completed
    live.execute(f'UPDATE kanban_actionability_commands SET {column}=?', (value,))
    assert not audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('status', ['done', 'completed'])
def test_writer_clocks_can_cross_seconds(completed, status):
    live, shadow, op = completed
    for conn in (live, shadow):
        conn.execute('UPDATE task_runs SET status=?,ended_at=126', (status,))
        conn.execute("UPDATE task_events SET created_at=127 WHERE kind='completed'")
    assert audit.audited_rejection(live, shadow, op)


@pytest.mark.parametrize('milliseconds,expected', [(125001, True), (125000, False), (124999, False)])
def test_same_second_requires_strict_portable_precision(milliseconds, expected):
    from hermes_cli.kanban_sync_actionability_terminal import _before

    def event(ms):
        uid = str(uuid.UUID(int=(ms << 80) | (7 << 76) | (2 << 62)))
        return {'uid': uid, 'created_at': 125, 'id': 999999-ms}

    assert _before(event(125000), event(milliseconds)) is expected
    assert not _before({'uid': 'legacy', 'created_at': 125}, event(125001))
