"""Confirmed diagnostic custody must compose into the source-derived report. [daveey]"""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_actionability_transport as act
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_actionability_cli import report
from hermes_cli.kanban_actionability_sources import describe
from tests.hermes_cli import test_kanban_actionability_transport as support

live = support.live


def confirmed(live):
    tid, payload = support.contract(live)
    source = live.execute("SELECT uid,created_at FROM task_events WHERE task_id=? AND kind='created'", (tid,)).fetchone()
    payload.update(first_observed=source['created_at'], deadline_at=source['created_at'] + 3600,
                   owner={'kind': 'assignee', 'refs': ['default']})
    payload['facts']['source_uids'] = [{'uid': source['uid'], 'version': 1}]
    with kbc.write_txn(live):
        owned = act.reserve(live, tid, payload)
    wire = json.loads(owned['request_json'])
    receipt = {'version': 1, 'request_uid': wire['client_op_id'],
               'request_sha256': act.digest(act.canonical(wire)),
               'home_gateway': 'fixture', 'contract': wire['payload']}
    row = {'uid': wire['contract_uid'], 'task_id': tid, 'kind': 'actionability_contract',
           'version': 1, 'deleted_at': None, 'origin_gateway': 'fixture', 'ord': 2,
           'created_at': source['created_at'], 'payload': json.dumps(receipt)}

    class Hub:
        ident = SimpleNamespace(gateway='fixture')

        def actionability_capability(self, board):
            assert not live.in_transaction
            return {'actionability_record': 1, 'frozen_replay': 1, 'diagnostic_only': True,
                    'hlc': '9999999999999-0000-server'}

        def ops(self, board, sent):
            assert sent == [wire]
            return {'results': [{'client_op_id': wire['client_op_id'], 'status': 'applied'}]}

        def row_evidence(self, board, table, uid):
            assert uid == wire['contract_uid']
            return {'board': board, 'table': table, 'uid': uid, 'state': 'live', 'row': row}

    task_before = dict(live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    runs_before = list(live.execute('SELECT * FROM task_runs WHERE task_id=?', (tid,)))
    unconfirmed = describe(live, tid, now=source['created_at'])
    result = act.confirm(live, Hub(), 'default', owned['op_id'])
    assert describe(live, tid, now=source['created_at'])['contract_key'] == unconfirmed['contract_key']
    assert result['delivery_state'] == 'confirmed' and result['execution_authority'] is False
    assert dict(live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == task_before
    assert list(live.execute('SELECT * FROM task_runs WHERE task_id=?', (tid,))) == runs_before
    task = kb.get_task(live, tid)
    assert task is not None and task.status == 'ready'
    current = act.command(live, owned['op_id'])
    assert current is not None and current['canonical_receipt_json'] == act.canonical(receipt)
    assert live.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='actionability_contract'", (tid,)).fetchone()
    return tid, payload, source, wire


def test_confirmed_contract_reaches_describe_and_report_without_releasing_scope(live):
    tid, payload, source, _ = confirmed(live)
    direct = describe(live, tid, now=source['created_at'])
    listed = report(live, now=source['created_at'])['rows'][0]
    for projection in (direct, listed):
        assert projection['escalation']['at'] == payload['deadline_at']
        assert 'escalation_deadline_missing' not in projection['violations']
        assert projection['evidence_state'] == 'unknown'  # confirmation is not current eligibility


@pytest.mark.parametrize('change', ['ack', 'pending', 'rejected', 'digest', 'receipt', 'receipt-list',
                                    'receipt-null', 'op-id', 'contract-key', 'event', 'home',
                                    'status', 'assignee', 'source', 'dependency', 'run'])
def test_unconfirmed_damaged_or_stale_contract_does_not_hide_violation(live, change):
    tid, _, source, wire = confirmed(live)
    parent = kb.create_task(live, title='new real prerequisite', assignee='default') if change == 'dependency' else None
    changes = {
        'ack': [("UPDATE kanban_actionability_commands SET delivery_state='acknowledged'", ())],
        'pending': [("UPDATE kanban_actionability_commands SET delivery_state='pending'", ())],
        'rejected': [("UPDATE kanban_actionability_commands SET delivery_state='rejected'", ())],
        'digest': [("UPDATE kanban_actionability_commands SET request_sha256='not-the-digest'", ())],
        'op-id': [("UPDATE kanban_actionability_commands SET op_id='different'", ())],
        'contract-key': [("UPDATE kanban_actionability_commands SET contract_key='different'", ())],
        'event': [('DELETE FROM task_events WHERE uid=?', (wire['contract_uid'],))],
        'home': [("UPDATE tasks SET created_on='other-home' WHERE id=?", (tid,))],
        'status': [("UPDATE tasks SET status='triage' WHERE id=?", (tid,))],
        'assignee': [("UPDATE tasks SET assignee='other-profile' WHERE id=?", (tid,))],
        'source': [("UPDATE task_events SET uid='different-source' WHERE uid=?", (source['uid'],))],
        'dependency': [('INSERT INTO task_links (parent_id,child_id) VALUES (?,?)', (parent, tid))],
        'run': [("INSERT INTO task_runs (task_id,profile,status,started_at) VALUES (?,'default','running',1)", (tid,)),
                ('UPDATE tasks SET current_run_id=(SELECT MAX(id) FROM task_runs WHERE task_id=?) WHERE id=?', (tid, tid))],
    }
    for label, raw in (('receipt', '{}'), ('receipt-list', '[]'), ('receipt-null', 'null')):
        changes[label] = [('UPDATE kanban_actionability_commands SET canonical_receipt_json=?', (raw,)),
                          ('UPDATE task_events SET payload=? WHERE uid=?', (raw, wire['contract_uid']))]
    with kbc.write_txn(live):
        for sql, params in changes[change]:
            live.execute(sql, params)
    for row in (describe(live, tid, now=source['created_at']),
                next(r for r in report(live, now=source['created_at'])['rows'] if r['task_id'] == tid)):
        assert row['escalation']['at'] is None
        assert 'escalation_deadline_missing' in row['violations']
        assert row['evidence_state'] == 'unknown'


def test_comment_and_clock_do_not_renew_confirmed_deadline_or_identity(live):
    tid, payload, source, _ = confirmed(live)
    before = describe(live, tid, now=source['created_at'])
    with kbc.write_txn(live):
        kb._append_event(live, tid, 'commented', {'author': 'default', 'len': 10})
    after = describe(live, tid, now=payload['deadline_at'] + 1)
    assert after['escalation']['at'] == before['escalation']['at'] == payload['deadline_at']
    assert after['contract_key'] == before['contract_key']
    assert after['release'] == before['release']


@pytest.mark.parametrize('status,deadline,state,violations,pending', [
    ('done', None, 'unavailable', ['escalation_deadline_missing'], None),
    ('ready', 200, 'unavailable', ['escalation_deadline_missing'], None),
    ('ready', None, 'not_applicable', ['escalation_deadline_missing'], None),
    ('ready', None, 'unavailable', [], None),
    ('ready', None, 'unavailable', ['escalation_deadline_missing'], {'request_uid': 'original-frozen-request'}),
])
def test_nonmissing_or_unresolved_deadline_is_not_rewritten(status, deadline, state, violations, pending):
    from hermes_cli.kanban_actionability import project
    from hermes_cli.kanban_actionability_confirmed import compose
    facts = {'task': {'id': 'root', 'status': 'ready', 'assignee': 'default'}, 'pending_command': pending}
    row = project(facts, now=100)
    row.update(status=status, violations=violations)
    row['escalation'].update(at=deadline, state=state)
    before = json.loads(json.dumps(row))
    # object() has no query API: all guards must run before touching optional custody.
    assert compose(object(), facts['task'], facts, row, now=100) == before
