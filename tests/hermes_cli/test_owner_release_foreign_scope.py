"""Foreign retained history at the real HOME processor (synthetic HTTP adapter).

The separate composed test covers actual HTTP/DO/CLI transport. Never install
foreign private command custody on HOME to satisfy a release guard.
"""
import copy
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_release_assemble as assemble
from hermes_cli import kanban_release_certificate as cert, kanban_release_home as home
from hermes_cli import kanban_scope_renewal as renewal
from tests.hermes_cli import test_owner_release_assemble as assembly_helpers
from tests.hermes_cli import test_owner_release_materialize as materialize_helpers

board = assembly_helpers.board
proof = assembly_helpers.proof
assembly = assembly_helpers.assembly


@pytest.fixture
def foreign(assembly, monkeypatch):
    from hermes_cli import kanban_operator_call as operator
    a = assembly
    with kb.write_txn(a.conn):
        rid = a.conn.execute("INSERT INTO task_runs(uid,task_id,profile,status,outcome,started_at,ended_at) "
                             "VALUES ('foreign-run',?,'default','done','retained',1,2)", (a.tid,)).lastrowid
        payload = dict(version=1, task_uid=a.tid, run_uid='foreign-run', request_uid='retain-request',
                       event_uid='restriction-1', episode_uid='stop-1', request_sha256='a' * 64,
                       operation_kind='retain', actor_gateway='executor', superseded_by_safety_stop=False,
                       disposition=dict(version=1, reason='scope_stopped', summary='Original scope',
                                        next_step='Original owner decides', release_condition='Check original scope', evidence=['proof']))
        a.conn.execute("UPDATE task_events SET kind='decision_retained',run_id=?,payload=?,origin_gateway='executor' "
                       "WHERE uid='restriction-1'", (rid, cert.encode(payload)))
        # No origin private command is imported along with the real public row.
        assert not a.conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_failure_commands'").fetchone()
    row = dict(a.conn.execute("SELECT * FROM task_events WHERE uid='restriction-1'").fetchone())
    canonical = {k: row.get(k) for k in cert._EVENT}
    canonical.update(run_uid='foreign-run', ord=1000, seq=1000, version=1,
                     updated_at=100000, hlc='100000-0000-executor', deleted_at=None)
    class Hub(materialize_helpers.Hub):
        def row_evidence(self, board, table, uid, **kwargs):
            assert not a.conn.in_transaction
            assert (board, table, uid) == ('example', 'task_events', 'restriction-1')
            return dict(board=board, table=table, uid=uid, state='live', row=copy.deepcopy(canonical))
    hub = Hub(a)
    monkeypatch.setattr(operator, '_board_slug', lambda conn: 'example')
    a.hub, a.canonical = hub, canonical
    return a


def process(a):
    row = a.conn.execute('SELECT * FROM owner_release_home_queue').fetchone()
    return home._process(a.conn, a.hub, row, 'home', 'example')


def test_foreign_retained_receipt_applies_without_local_command_custody(foreign):
    a = foreign
    assert [renewal.key(e) for e in renewal.scope_evidence(a.conn, a.tid, 'stop-1')] == ['stop-1']
    result = process(a)
    assert result['result']['outcome'] == 'released'
    assert len(a.hub.posts) == 1
    assert process(a) == result
    assert len(a.hub.posts) == 1
    assert [renewal.key(e) for e in renewal.scope_evidence(a.conn, a.tid, 'stop-1')] == ['stop-1']


def test_well_formed_local_receipt_drift_refuses_before_final_wire(foreign):
    a = foreign
    p = json.loads(a.canonical['payload'])
    p['disposition']['summary'] = 'Changed but otherwise valid'
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
    before = assembly_helpers.snapshot(a)
    with pytest.raises(ValueError):
        process(a)
    assert assembly_helpers.snapshot(a) == before
    assert not a.hub.posts


def test_uncaptured_consumption_does_not_qualify_foreign_receipt(foreign):
    a = foreign
    with pytest.raises(ValueError):
        assemble.apply(a.conn, a.proof, delivery_uid='delivery-1')


@pytest.mark.parametrize('field,value', [
    ('event_uid', 'another'), ('task_uid', 'another'), ('episode_uid', 'another'),
    ('run_uid', 'another'), ('actor_gateway', 'another'), ('operation_kind', 'release'),
    ('version', True), ('request_uid', ''), ('request_sha256', 'g' * 64),
    ('superseded_by_safety_stop', True), ('superseded_by_safety_stop', 0),
    ('disposition', []), ('extra', 'unknown'),
])
def test_foreign_payload_damage_has_no_effects(foreign, field, value):
    a = foreign
    p = json.loads(a.canonical['payload'])
    p[field] = value
    # Both copies agree: semantic checks must refuse, not just byte equality.
    a.canonical['payload'] = cert.encode(p)
    with kb.write_txn(a.conn):
        a.conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
    before = assembly_helpers.snapshot(a)
    with pytest.raises(ValueError):
        process(a)
    assert assembly_helpers.snapshot(a) == before
    assert not a.hub.posts


@pytest.mark.parametrize('damage', ['json', 'missing', 'run_missing', 'run_task', 'run_live', 'run_outcome', 'home_origin', 'new_receipt'])
def test_foreign_history_damage_has_no_effects(foreign, damage):
    a = foreign
    with kb.write_txn(a.conn):
        damage_sql = {
            'json': "UPDATE task_events SET payload='invalid' WHERE uid='restriction-1'",
            'missing': "DELETE FROM task_events WHERE uid='restriction-1'",
            'run_missing': "UPDATE task_events SET run_id=NULL WHERE uid='restriction-1'",
            'run_live': "UPDATE task_runs SET ended_at=NULL WHERE uid='foreign-run'",
            'run_outcome': "UPDATE task_runs SET outcome='completed' WHERE uid='foreign-run'",
        }
        if damage in damage_sql:
            a.conn.execute(damage_sql[damage])
        elif damage == 'run_task':
            other = kb.create_task(a.conn, title='unrelated run', assignee='default')
            a.conn.execute("UPDATE task_runs SET task_id=? WHERE uid='foreign-run'", (other,))
        elif damage == 'home_origin':
            p = json.loads(a.canonical['payload'])
            p['actor_gateway'] = 'home'
            a.canonical.update(origin_gateway='home', payload=cert.encode(p))
            a.conn.execute("UPDATE task_events SET origin_gateway='home',payload=? WHERE uid='restriction-1'", (cert.encode(p),))
        else:
            p = json.loads(a.canonical['payload'])
            p['event_uid'] = 'new-retain'
            row = a.conn.execute("SELECT run_id FROM task_events WHERE uid='restriction-1'").fetchone()
            a.conn.execute("INSERT INTO task_events(uid,task_id,run_id,kind,payload,created_at,origin_gateway) "
                           "VALUES ('new-retain',?,?,'decision_retained',?,90,'executor')", (a.tid, row[0], cert.encode(p)))
    before = assembly_helpers.snapshot(a)
    with pytest.raises(ValueError):
        process(a)
    assert assembly_helpers.snapshot(a) == before
    assert not a.hub.posts


@pytest.mark.parametrize('field,value', [('board', 'other'), ('table', 'tasks'), ('uid', 'other'), ('state', 'deleted'),
                                        ('row', None), ('deleted_at', 1), ('version', True), ('version', 2),
                                        ('created_at', 91), ('run_uid', 'other'), ('origin_gateway', 'other')])
def test_canonical_evidence_binding_refuses(foreign, field, value):
    a = foreign
    actual = a.hub.row_evidence
    def changed(*args, **kwargs):
        reply = actual(*args, **kwargs)
        target = reply if field in {'board', 'table', 'uid', 'state', 'row'} else reply['row']
        target[field] = value
        return reply
    a.hub.row_evidence = changed
    before = assembly_helpers.snapshot(a)
    with pytest.raises(ValueError):
        process(a)
    assert assembly_helpers.snapshot(a) == before
    assert not a.hub.posts


def test_scope_rechecked_after_real_budget_body_and_rolls_back(foreign, monkeypatch):
    a = foreign
    original = assemble._budget
    def changed(conn, body, seen):
        mandate = original(conn, body, seen)
        p = json.loads(a.canonical['payload'])
        p['disposition']['summary'] = 'Raced during actual proposal assembly'
        conn.execute("UPDATE task_events SET payload=? WHERE uid='restriction-1'", (cert.encode(p),))
        return mandate
    monkeypatch.setattr(assemble, '_budget', changed)
    before = assembly_helpers.snapshot(a)
    with pytest.raises(ValueError):
        process(a)
    assert assembly_helpers.snapshot(a) == before
    assert not a.hub.posts


def test_canonical_unavailable_does_not_freeze_or_remint(foreign):
    a = foreign
    def unavailable(*args, **kwargs):
        raise OSError('unavailable')
    a.hub.row_evidence = unavailable
    before = assembly_helpers.snapshot(a)
    with pytest.raises(OSError):
        process(a)
    assert assembly_helpers.snapshot(a) == before
    assert not a.hub.posts
