"""A fenced phantom event stays tombstoned, but its rejected upload can retire."""
import copy
import json

import pytest

from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_live_claim_child_rows as child
from tests.hermes_cli.test_kanban_remote_archive import _running_on_worker

svc = child.svc
gws = child.gws


@pytest.mark.parametrize('table', ['task_events', 'task_runs'])
def test_normal_round_retires_fenced_event_and_keeps_audit_and_tombstone(gws, svc, table):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    child._old_core_complete(hub.conn, tid, phantom=table == 'task_runs')
    op = dict(hub.conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name=? "
                               "AND (?='task_runs' OR json_extract(fields,'$.kind')='completed')",
                               (table, table)).fetchone())
    hub.round()
    uid = op['row_uid']
    tomb = tuple(hub.conn.execute("SELECT * FROM kanban_sync_tombstones WHERE table_name=? "
                                  "AND row_uid=?", (table, uid)).fetchone())
    audits = [tuple(r) for r in hub.conn.execute("SELECT uid,payload FROM task_events WHERE kind='sync_rejected'")
              if json.loads(r['payload']).get('op_id') == op['op_id']]
    assert len(audits) == 1
    hub.round()
    assert hub.conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id=?", (op['op_id'],)).fetchone() is None
    assert tuple(hub.conn.execute("SELECT * FROM kanban_sync_tombstones WHERE table_name=? "
                                  "AND row_uid=?", (table, uid)).fetchone()) == tomb
    assert [tuple(r) for r in hub.conn.execute("SELECT uid,payload FROM task_events WHERE kind='sync_rejected'")
            if json.loads(r['payload']).get('op_id') == op['op_id']] == audits
    assert uid not in svc.board(child.BOARD)['rows'][table]
    assert svc.board(child.BOARD)['leases'][tid]['holder'] == child.WORKER
    assert client.status(hub.conn)['rejected'] == 0


@pytest.fixture(params=['task_events', 'task_runs'])
def refused(gws, svc, monkeypatch, request):
    from hermes_cli import kanban_sync_rejected_event as event
    from hermes_cli.kanban_sync_rejections import _inputs
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    table = request.param
    child._old_core_complete(hub.conn, tid, phantom=table == 'task_runs')
    with monkeypatch.context() as patch:
        patch.setattr(event, 'moot_event', lambda *args: False)
        hub.round()
    op = dict(hub.conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name=? "
                               "AND state='rejected'", (table,)).fetchone())
    before = _inputs(hub.conn, op)
    evidence = svc.hub(child.HUB).row_evidence(child.BOARD, table, op['row_uid'])
    assert event.moot_event(op, before, evidence)
    return hub, op, before, evidence


VETOES = [
    'table', 'operation', 'reason', 'shadow', 'row', 'parent', 'parent_ops', 'pending', 'receipt',
    'no_tomb', 'parent_tomb', 'wrong_tomb', 'no_audit', 'duplicate_audit', 'audit_table', 'audit_uid',
    'audit_reason', 'audit_fields', 'task_id', 'remote_live', 'floor', 'remote_row', 'remote_tomb',
]


def assert_retirement_predicate_fails_closed(refused, veto):
    from hermes_cli.kanban_sync_rejected_event import moot_event
    _, op, before, evidence = copy.deepcopy((None, *refused[1:]))
    if veto in ('table', 'operation', 'reason'):
        op[{'table': 'table_name', 'operation': 'op', 'reason': 'last_error'}[veto]] = 'wrong'
        if veto == 'table':
            before['tombstones'][0] = ('wrong', op['row_uid'], before['tombstones'][0][2])
            audit = before['fenced_event']['audits'][0]
            payload = json.loads(audit['payload'])
            payload['table'] = 'wrong'
            audit['payload'] = json.dumps(payload)
        if veto == 'reason':
            audit = before['fenced_event']['audits'][0]
            payload = json.loads(audit['payload'])
            payload['reason'] = 'wrong'
            audit['payload'] = json.dumps(payload)
    elif veto == 'shadow':
        before['fenced_event']['mode'] = 'shadow'
    elif veto == 'row':
        before['row'] = {'uid': op['row_uid']}
    elif veto == 'parent':
        before['parent'] = None
    elif veto in ('parent_ops', 'pending', 'receipt'):
        before[veto] = ['unsettled']
    elif veto == 'no_tomb':
        before['tombstones'] = []
    elif veto == 'parent_tomb':
        before['tombstones'].append(('tasks', before['task'], 1))
    elif veto == 'wrong_tomb':
        before['tombstones'] = [('tasks', before['task'], 1)]
    elif veto == 'no_audit':
        before['fenced_event']['audits'] = []
    elif veto == 'duplicate_audit':
        before['fenced_event']['audits'] *= 2
    elif veto.startswith('audit_'):
        audit = before['fenced_event']['audits'][0]
        payload = json.loads(audit['payload'])
        payload[veto.removeprefix('audit_')] = 'wrong'
        audit['payload'] = json.dumps(payload)
    elif veto == 'task_id':
        fields = json.loads(op['fields'])
        fields['task_id'] = 'another'
        op['fields'] = json.dumps(fields)
    else:
        key, value = {'remote_live': ('state', 'live'), 'floor': ('floor', 1),
                      'remote_row': ('row', {}), 'remote_tomb': ('tombstone', {})}[veto]
        evidence[key] = value
    assert not moot_event(op, before, evidence), veto


def assert_evidence_read_revalidates_retirement_provenance(refused, svc, monkeypatch, race, target_first):
    from hermes_cli.kanban_sync_rejections import resolve_rejections
    hub, op, before, _ = refused
    # The phantom-run fixture also rejects its completed event. Exercise both
    # orders explicitly instead of depending on random outbox operation IDs.
    ordered_id = '0000-target' if target_first else 'zzzz-target'
    with hub.conn:
        hub.conn.execute("UPDATE kanban_sync_outbox SET op_id=? WHERE op_id=?", (ordered_id, op['op_id']))
        hub.conn.execute("UPDATE task_events SET payload=json_set(payload, '$.op_id', ?) WHERE uid=?",
                         (ordered_id, before['fenced_event']['audits'][0]['uid']))
    op['op_id'] = ordered_id
    transport = svc.hub(child.HUB)
    original = transport.row_evidence
    reads = []
    injected = []
    target = (child.BOARD, op['table_name'], op['row_uid'])

    def read(*args):
        evidence = original(*args)
        reads.append(args)
        if args != target:
            return evidence
        injected.append(args)
        with hub.conn:
            if race == 'audit':
                hub.conn.execute("UPDATE task_events SET payload='{}' WHERE uid=?",
                                 (before['fenced_event']['audits'][0]['uid'],))
            elif race == 'mode':
                hub.conn.execute("UPDATE kanban_sync_state SET mode='shadow'")
            else:
                hub.conn.execute("DELETE FROM kanban_sync_tombstones WHERE table_name=? AND row_uid=?",
                                 (op['table_name'], op['row_uid']))
        return evidence

    monkeypatch.setattr(transport, 'row_evidence', read)
    report = resolve_rejections(hub.conn, transport, child.BOARD)
    assert injected == [target]
    if op['table_name'] == 'task_runs':
        assert (reads[0] == target) is target_first
    target_report = next(item for item in report if item['op_id'] == op['op_id'])
    assert target_report['action'] == 'retained'
    assert target_report['proof'] == 'local state changed during evidence read'
    assert hub.conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE op_id=?", (op['op_id'],)).fetchone()


def test_each_allow_more_guard_removal_is_killed(refused, monkeypatch):
    """Adjacent-valid controls must detect removing each atom, not just inverting the whole guard."""
    import ast
    import inspect
    from hermes_cli import kanban_sync_rejected_event as event
    source = ast.parse(inspect.getsource(event.moot_event))
    guard = next(n for n in ast.walk(source) if isinstance(n, ast.If))
    success = next(n for n in ast.walk(source) if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.And))
    vetoes = VETOES
    for node, value in [(guard.test, False), (success, True)]:
        for index in range(len(node.values)):
            tree = copy.deepcopy(source)
            target = next(n for n in ast.walk(tree) if isinstance(n, ast.BoolOp)
                          and n.lineno == node.lineno and n.col_offset == node.col_offset)
            target.values[index] = ast.Constant(value=value)
            namespace = dict(event.__dict__)
            exec(compile(ast.fix_missing_locations(tree), '<guard-removal>', 'exec'), namespace)
            with monkeypatch.context() as patch:
                patch.setattr(event, 'moot_event', namespace['moot_event'])
                killed = False
                for veto in vetoes:
                    try:
                        assert_retirement_predicate_fails_closed(refused, veto)
                    except (AssertionError, IndexError):
                        killed = True
                        break
                assert killed, f'guard removal survived: {ast.unparse(node.values[index])}'
