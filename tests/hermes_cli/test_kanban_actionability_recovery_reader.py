"""Owner and overdue receipt composition uses confirmed custody, never release authority."""
import json

import pytest

from hermes_cli import kanban_actionability_recovery as recovery
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_actionability_cli import report
from tests.hermes_cli import test_kanban_actionability_recovery as support
from tests.hermes_cli import test_kanban_actionability_observation as observations

live = support.live


def test_confirmed_parent_owner_fills_only_missing_administrative_owner(live):
    tid, hub, obligation = support.setup(live)
    parent = kb.create_task(live, title='accountable recovery parent', assignee='default')
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET assignee=NULL WHERE id=?', (tid,))
        live.execute('INSERT INTO task_links (parent_id,child_id) VALUES (?,?)', (parent, tid))
    hub.data[tid]['task']['assignee'] = None
    hub.data[parent] = observations.inventory(parent)
    hub.pages[0].update(rows=[{'uid': 'link', 'child_id': tid, 'parent_id': parent}], next='link')
    obligation['owner'] = {'kind': 'task', 'refs': [parent]}
    value = support.preview(live, hub, tid, obligation)
    before = describe(live, tid, now=hub.now)
    assert 'owner_missing' in before['violations']
    support.apply(live, hub, value)
    for row in (describe(live, tid, now=hub.now), next(r for r in report(live, now=hub.now)['rows'] if r['task_id'] == tid)):
        assert row['owner'] == obligation['owner']
        assert 'owner_missing' not in row['violations']
        assert row['evidence_state'] == 'unknown'
        assert row['release'] == before['release'] and row['action'] == before['action']


def test_competing_confirmed_owners_stay_unknown_without_renewing_earliest_deadline(live):
    tid, hub, obligation = support.setup(live)
    parents = [kb.create_task(live, title='accountable parent', assignee='default') for _ in range(2)]
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET assignee=NULL WHERE id=?', (tid,))
        live.executemany('INSERT INTO task_links (parent_id,child_id) VALUES (?,?)', [(p, tid) for p in parents])
    hub.data[tid]['task']['assignee'] = None
    for pid in parents:
        hub.data[pid] = observations.inventory(pid)
    hub.pages[0].update(rows=[{'uid': f'link{i}', 'child_id': tid, 'parent_id': pid}
                              for i, pid in enumerate(parents)], next='link1')
    for i, pid in enumerate(parents):
        current = {**obligation, 'owner': {'kind': 'task', 'refs': [pid]},
                   'deadline_at': obligation['deadline_at'] + i * 100}
        support.apply(live, hub, support.preview(live, hub, tid, current))
    row = describe(live, tid, now=hub.now)
    assert row['owner']['kind'] == 'unavailable' and 'owner_missing' in row['violations']
    assert row['escalation']['at'] == obligation['deadline_at']
    assert row['evidence_state'] == 'unknown'


def blocked(conn):
    tid, hub, obligation = support.setup(conn)
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'blocked', {'reason': 'actual recorded gate'})
    event = conn.execute("SELECT uid,created_at FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
    hub.data[tid]['task']['status'] = 'blocked'
    hub.data[tid]['events'].append({'uid': event['uid'], 'task_id': tid, 'kind': 'blocked', 'ord': 2,
                                   'version': 1, 'created_at': event['created_at'], 'deleted_at': None})
    value = support.preview(conn, hub, tid, obligation)
    support.apply(conn, hub, value)
    hub.now = obligation['deadline_at']
    assert 'escalation_overdue' in describe(conn, tid, now=hub.now)['violations']
    return tid, hub, value


@pytest.mark.parametrize('damage', [None, 'origin', 'deleted', 'wire', 'envelope', 'destination'])
def test_recorded_receipt_clears_only_overdue_with_all_linkage_intact(live, damage):
    tid, hub, value = blocked(live)
    hub.destination_kind = 'chat'
    before = describe(live, tid, now=hub.now)
    result = recovery.escalate(live, hub, 'default', value['op_id'], value['request_sha256'])
    wire = hub.sent[-1]
    if damage:
        with kbc.write_txn(live):
            if damage == 'origin':
                live.execute("UPDATE task_events SET origin_gateway='foreign' WHERE uid=?", (wire['notice_uid'],))
            elif damage == 'deleted':
                # Core's schema has no deleted_at event column: deletion is the local tombstone representation.
                live.execute('DELETE FROM task_events WHERE uid=?', (wire['notice_uid'],))
            elif damage == 'wire':
                live.execute("UPDATE kanban_actionability_commands SET request_sha256='tampered' WHERE phase='escalation'")
            else:
                receipt = json.loads(live.execute("SELECT canonical_receipt_json FROM kanban_actionability_commands WHERE phase='escalation'").fetchone()[0])
                if damage == 'envelope':
                    receipt['envelope']['deadline_at'] += 1
                else:
                    receipt['destination']['task_id'] = 'unrelated'
                raw = json.dumps(receipt)
                live.execute("UPDATE kanban_actionability_commands SET canonical_receipt_json=? WHERE phase='escalation'", (raw,))
                live.execute('UPDATE task_events SET payload=? WHERE uid=?', (raw, wire['receipt_uid']))
    row = describe(live, tid, now=hub.now)
    assert ('escalation_overdue' in row['violations']) == bool(damage)
    assert row['escalation']['receipt_uid'] == (None if damage else result['receipt_uid'])
    assert row['evidence_state'] == 'unknown' and row['release'] == before['release']
    assert row['action'] == before['action'] and row['contract_key'] == before['contract_key']


def test_existing_source_deadline_does_not_consume_other_deadline_receipt(live):
    from hermes_cli.kanban_actionability_confirmed import compose
    from hermes_cli.kanban_actionability_sources import Reader
    tid, hub, value = blocked(live)
    hub.destination_kind = 'chat'
    recovery.escalate(live, hub, 'default', value['op_id'], value['request_sha256'])
    row = describe(live, tid, now=hub.now)
    row['escalation'].update(at=hub.now + 100, state='pending', receipt_uid=None)
    row['violations'].append('escalation_overdue')
    reader = Reader(live)
    facts = {'source_refs': [value['request']['payload']['facts']['source_uids'][0]['uid']], 'dependencies': []}
    result = compose(reader, reader.task(tid), facts, row, now=hub.now + 101)
    assert result['escalation']['at'] == hub.now + 100 and result['escalation']['receipt_uid'] is None
    assert 'escalation_overdue' in result['violations']


@pytest.mark.parametrize('damage', [None, 'ack', 'receipt', 'notice', 'event'])
def test_confirmed_route_unavailable_stays_overdue_but_exposes_exact_receipt(live, damage):
    tid, hub, value = blocked(live)
    result = recovery.escalate(live, hub, 'default', value['op_id'], value['request_sha256'])
    wire = hub.sent[-1]
    if damage:
        with kbc.write_txn(live):
            if damage == 'ack':
                live.execute("UPDATE kanban_actionability_commands SET delivery_state='acknowledged' WHERE phase='escalation'")
            elif damage == 'receipt':
                live.execute("UPDATE kanban_actionability_commands SET canonical_receipt_json='{}' WHERE phase='escalation'")
            else:
                uid = wire['notice_uid'] if damage == 'notice' else wire['receipt_uid']
                live.execute('DELETE FROM task_events WHERE uid=?', (uid,))
    for row in (describe(live, tid, now=hub.now), next(r for r in report(live, now=hub.now)['rows'] if r['task_id'] == tid)):
        assert row['escalation']['at'] == value['request']['payload']['deadline_at']
        assert row['escalation']['receipt_uid'] == (None if damage else result['receipt_uid'])
        assert row['escalation']['state'] == ('pending' if damage else 'route_unavailable')
        assert 'escalation_overdue' in row['violations']
        assert row['evidence_state'] == 'unknown'
