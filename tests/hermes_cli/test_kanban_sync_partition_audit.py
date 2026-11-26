"""Foreign parents are evidence only inside the authenticated fresh report cut."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_actionability_drift as diagnostic
from hermes_cli import kanban_sync_refusal_audit as audit
from hermes_cli.kanban_history_residue_source import Refused

pytest_plugins = ['tests.hermes_cli.test_kanban_sync_actionability_drift']


@pytest.fixture
def partition(stores):
    live, shadow, op = stores
    for conn in (live, shadow):
        conn.execute('CREATE TABLE kanban_sync_state(board_id TEXT, gateway_id TEXT, cursor INTEGER)')
        conn.execute("INSERT INTO kanban_sync_state VALUES ('default','macbook',100)")
        conn.execute("UPDATE tasks SET created_on='foreign' WHERE id='t_parent'")
    live.execute("DELETE FROM tasks WHERE id='t_parent'")
    for conn in (live, shadow):
        conn.commit()
    hub = SimpleNamespace(ident=SimpleNamespace(url='https://example.test', gateway='macbook', key='key'),
                          changes=lambda *a: dict(changes=[], more=False, head=100, next=100))
    return live, shadow, op, hub


def test_partition_report_preserves_custody_and_plain_diff_stays_dirty(partition):
    from hermes_cli.kanban_sync_client import diff

    live, shadow, op, hub = partition
    before = [list(c.iterdump()) for c in (live, shadow)]
    assert diagnostic._source(live, 't_card') is None
    assert not diagnostic.audited_rejection(live, shadow, op)
    assert not diff(live, shadow, 'macbook')['clean']
    result = audit.report(live, shadow, hub, 'default')
    assert result['clean'], result
    assert result['audited_retain_rejections'] == [op['op_id']]
    assert result['rejected_ops'] == 1
    assert [list(c.iterdump()) for c in (live, shadow)] == before


@pytest.mark.parametrize('fault', [
    'missing_parent', 'own_parent', 'unknown_owner', 'deleted_parent', 'unversioned_parent',
    'live_disagreement', 'source_disagreement', 'missing_original', 'custody', 'returned_premise',
    'empty_owner', 'zero_version', 'negative_version', 'text_version',
])
def test_partition_unknown_or_disagreeing_evidence_stays_dirty(partition, fault):
    live, shadow, _op, hub = partition
    statements = {
        'missing_parent': (shadow, "DELETE FROM tasks WHERE id='t_parent'"),
        'own_parent': (shadow, "UPDATE tasks SET created_on='macbook' WHERE id='t_parent'"),
        'unknown_owner': (shadow, "UPDATE tasks SET created_on=NULL WHERE id='t_parent'"),
        'deleted_parent': (shadow, "UPDATE tasks SET deleted_at=1 WHERE id='t_parent'"),
        'unversioned_parent': (shadow, "UPDATE tasks SET sync_version=NULL WHERE id='t_parent'"),
        'live_disagreement': (live, "INSERT INTO tasks VALUES ('t_parent',NULL,'ready','default',NULL,NULL,1,'foreign')"),
        'source_disagreement': (shadow, "INSERT INTO task_events VALUES (2,'new','t_card','linked',124,'{}',NULL)"),
        'missing_original': (live, "DELETE FROM task_events"),
        'custody': (live, "UPDATE kanban_actionability_commands SET request_sha256='bad'"),
        'empty_owner': (shadow, "UPDATE tasks SET created_on='' WHERE id='t_parent'"),
        'zero_version': (shadow, "UPDATE tasks SET sync_version=0 WHERE id='t_parent'"),
        'negative_version': (shadow, "UPDATE tasks SET sync_version=-1 WHERE id='t_parent'"),
        'text_version': (shadow, "UPDATE tasks SET sync_version='unknown' WHERE id='t_parent'"),
        'returned_premise': (shadow, "UPDATE tasks SET status='running' WHERE id='t_parent'"),
    }
    conn, sql = statements[fault]
    conn.execute(sql)
    conn.commit()
    result = audit.report(live, shadow, hub, 'default')
    assert not result['clean']
    assert not result['audited_retain_rejections']


@pytest.mark.parametrize('fault', ['cursor', 'source', 'feed'])
def test_partition_cut_mutation_refuses_before_audit(partition, fault):
    live, shadow, _op, hub = partition
    calls = []
    def changes(*args):
        calls.append(args)
        if len(calls) == 2:
            if fault == 'cursor':
                shadow.execute('UPDATE kanban_sync_state SET cursor=101')
                shadow.commit()
            elif fault == 'source':
                live.execute("UPDATE task_events SET payload=?", (json.dumps({'changed': True}),))
                live.commit()
            else:
                return dict(changes=[], more=False, head=101, next=101)
        return dict(changes=[], more=False, head=100, next=100)
    hub.changes = changes
    with pytest.raises(Refused):
        audit.report(live, shadow, hub, 'default')


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_partition_contained_parent_does_not_satisfy_frozen_triage(partition, status):
    from tests.hermes_cli.test_kanban_sync_actionability_drift import _refreeze

    live, shadow, op, hub = partition
    facts = json.loads(op['fields'])['payload']['facts']
    facts['dependencies'][0]['status'] = 'triage'
    _refreeze(live, op, facts)
    live.execute('UPDATE kanban_sync_outbox SET op_id=?,fields=?', (op['op_id'], op['fields']))
    shadow.execute("UPDATE tasks SET status=?,containment_episode='held' WHERE id='t_parent'", (status,))
    for conn in (live, shadow):
        conn.commit()
    result = audit.report(live, shadow, hub, 'default')
    assert not result['clean']
    assert not result['audited_retain_rejections']


def test_missing_replica_parent_returns_no_evidence(partition):
    _live, shadow, _op, _hub = partition
    shadow.execute("DELETE FROM tasks WHERE id='t_parent'")
    assert diagnostic._foreign_parent(shadow, 't_parent', 'macbook') is None


def test_partition_new_source_cannot_replace_missing_original(partition):
    live, shadow, _op, hub = partition
    for conn in (live, shadow):
        conn.execute("INSERT INTO task_events VALUES (2,'new-source','t_card','linked',124,'{}',NULL)")
    live.execute("DELETE FROM task_events WHERE uid='source'")
    for conn in (live, shadow):
        conn.commit()
    result = audit.report(live, shadow, hub, 'default')
    assert not result['clean']
    assert not result['audited_retain_rejections']


def test_partition_candidate_survives_large_verified_inventory(partition):
    from tests.hermes_cli.test_kanban_refusal_inventory import seed_retains

    live, shadow, op, hub = partition
    for conn in (live, shadow):
        conn.execute('ALTER TABLE task_runs ADD COLUMN ended_at INTEGER')
    seed_retains(live, shadow)
    for conn in (live, shadow):
        conn.commit()
    before = [list(c.iterdump()) for c in (live, shadow)]
    result = audit.report(live, shadow, hub, 'default')
    assert result['clean'] and result['rejected_ops'] == 25
    assert op['op_id'] in result['audited_retain_rejections']
    assert len(result['audited_retain_rejections']) == 25
    assert [list(c.iterdump()) for c in (live, shadow)] == before

