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
from tests.hermes_cli.kanban_legacy_command_support import seed_legacy_command

live = support.live


def confirmed(live, *, parent=None):
    tid, payload = support.contract(live)
    if parent is not None:
        live.execute('INSERT INTO task_links (parent_id,child_id) VALUES (?,?)', (parent, tid))
        payload['facts']['dependencies'] = [{'id': parent, 'status': 'todo'}]
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
        if change in ('op-id', 'contract-key'):
            column = {'op-id': 'op_id', 'contract-key': 'contract_key'}[change]
            seed_legacy_command(live, **{column: 'different'})
        else:
            for sql, params in changes[change]:
                live.execute(sql, params)
    # The ordinary ready fixture is runnable, so its missing deadline is no violation
    # (not_required). Custody is the point: no unconfirmed or stale contract may fill it,
    # and every card that still waits keeps its missing-deadline violation.
    waits = change in ('status', 'dependency')
    for row in (describe(live, tid, now=source['created_at']),
                next(r for r in report(live, now=source['created_at'])['rows'] if r['task_id'] == tid)):
        assert row['escalation']['at'] is None
        assert wire['contract_uid'] not in row['source_refs']
        assert ('escalation_deadline_missing' in row['violations']) is waits
        assert row['escalation']['state'] == ('unavailable' if waits else 'not_required')
        assert row['evidence_state'] == 'unknown'


@pytest.mark.parametrize('status', ['todo', 'running', 'done', 'archived', 'triage', 'missing'])
def test_parent_status_does_not_renew_or_erase_fixed_obligation(live, status):
    parent = kb.create_task(live, title='existing prerequisite', assignee='default')
    live.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent,))
    tid, payload, source, _ = confirmed(live, parent=parent)
    if status == 'missing':
        # A foreign parent can be absent from a partial replica; retain the link.
        live.execute('PRAGMA foreign_keys=OFF')
        live.execute('DELETE FROM tasks WHERE id=?', (parent,))
    else:
        live.execute('UPDATE tasks SET status=? WHERE id=?', (status, parent))
    before = list(live.iterdump())
    for row in (describe(live, tid, now=source['created_at']),
                next(r for r in report(live, now=source['created_at'])['rows'] if r['task_id'] == tid)):
        assert row['escalation']['at'] == payload['deadline_at']
        assert row['evidence_state'] == 'unknown'
        assert 'escalation_deadline_missing' not in row['violations']
    assert list(live.iterdump()) == before


def test_new_parent_stop_remains_visible_with_fixed_obligation(live):
    parent = kb.create_task(live, title='protected prerequisite', assignee='default')
    live.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent,))
    tid, payload, source, _ = confirmed(live, parent=parent)
    kb._append_event(live, parent, 'spend_budget_triaged', {'cause': 'scope_stopped'})
    with_contract = describe(live, tid, now=source['created_at'])
    live.execute("UPDATE kanban_actionability_commands SET delivery_state='pending' WHERE task_id=?", (tid,))
    without_contract = describe(live, tid, now=source['created_at'])
    assert with_contract['escalation']['at'] == payload['deadline_at']
    for key in ('action', 'blocker', 'release', 'dependencies', 'evidence_state'):
        assert with_contract[key] == without_contract[key]
    assert any(dep['task_id'] == parent for dep in with_contract['dependencies'])


def test_removed_parent_invalidates_fixed_obligation(live):
    parent = kb.create_task(live, title='old prerequisite', assignee='default')
    live.execute("UPDATE tasks SET status='todo' WHERE id=?", (parent,))
    tid, _, source, _ = confirmed(live, parent=parent)
    live.execute('DELETE FROM task_links WHERE child_id=?', (tid,))
    assert describe(live, tid, now=source['created_at'])['escalation']['at'] is None


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
