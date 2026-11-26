"""Bound unresolved work, not independently custody-verified historical audits."""
import pytest

from hermes_cli import kanban_sync_refusal_audit as audit
from hermes_cli.kanban_history_residue_source import Refused
from tests.hermes_cli import test_kanban_sync_refusal_audit as helpers

case = helpers.case


def seed_retains(live, shadow):
    import hashlib
    import json

    for conn in (live, shadow):
        conn.execute("INSERT INTO task_runs VALUES (1,'run','t_card',123)")
    live.execute('''CREATE TABLE kanban_failure_commands(
        run_uid TEXT, task_id TEXT, op_id TEXT, operation_kind TEXT,
        request_uid TEXT, event_uid TEXT, request_json TEXT, request_sha256 TEXT,
        delivery_state TEXT, canonical_receipt_json TEXT)''')
    for i in range(24):
        uid = f'retain-{i:02}'
        wire = dict(op='retain', client_op_id=uid, task='t_card', run_uid='run', event_uid='event')
        text = json.dumps(wire, sort_keys=True, separators=(',', ':'))
        # Each immutable command needs its own portable run identity.
        run = f'run-{i}'
        for conn in (live, shadow):
            conn.execute('INSERT INTO task_runs VALUES (?,?,?,123)', (i + 2, run, 't_card'))
        wire['run_uid'] = run
        text = json.dumps(wire, sort_keys=True, separators=(',', ':'))
        live.execute('INSERT INTO kanban_failure_commands VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (run, 't_card', uid, 'retain', uid, 'event', text,
                      hashlib.sha256(text.encode()).hexdigest(), 'rejected', None))
        live.execute('INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,state,last_error) '
                     'VALUES (?,?,?,?,?,?,?)',
                     (uid, 'tasks', 't_card', 'retain', text, 'rejected', 'missing_decision_episode'))


def test_verified_retains_do_not_exhaust_unresolved_inventory(case):
    (live, shadow), hub, _proof, calls = case
    seed_retains(live, shadow)
    before = [list(c.iterdump()) for c in (live, shadow)]
    result = audit.report(live, shadow, hub, 'default')
    assert result['clean'] and result['rejected_ops'] == 25
    assert len(result['audited_retain_rejections']) == 24
    assert calls == ['rejected-op']
    assert [list(c.iterdump()) for c in (live, shadow)] == before
    shadow.execute("UPDATE tasks SET title='real drift'")
    assert not audit.report(live, shadow, hub, 'default')['clean']


@pytest.mark.parametrize('count', [19, 20])
def test_unknown_rows_still_consume_inventory(case, count):
    (live, shadow), hub, _proof, calls = case
    seed_retains(live, shadow)
    for i in range(count):
        live.execute('INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,state,last_error) '
                     'VALUES (?,?,?,?,?,?,?)',
                     (f'unknown-{i}', 'tasks', 't_card', 'upsert', '{}', 'rejected', 'unknown'))
    if count == 20:
        with pytest.raises(Refused, match='refusal_inventory_bound'):
            audit.report(live, shadow, hub, 'default')
        assert calls == []
    else:
        assert not audit.report(live, shadow, hub, 'default')['clean']
        assert calls == ['rejected-op']


def test_verified_custody_change_during_http_fails_closed(case, monkeypatch):
    (live, shadow), hub, _proof, _calls = case
    seed_retains(live, shadow)
    original = audit._receipt
    def receipt(*args):
        result = original(*args)
        live.execute("UPDATE kanban_failure_commands SET request_sha256='tampered'")
        return result
    monkeypatch.setattr(audit, '_receipt', receipt)
    with pytest.raises(Refused, match='local_cut_inputs_changed'):
        audit.report(live, shadow, hub, 'default')


@pytest.mark.parametrize('phase', ['initial', 'final'])
def test_inventory_scan_checks_original_ttl_per_row(case, monkeypatch, phase):
    (live, shadow), hub, _proof, _calls = case
    seed_retains(live, shadow)
    from hermes_cli import kanban_sync_retain_drift as retain
    predicate = retain.audited_rejection
    classified = []
    def classify(*args):
        classified.append(args[2]['op_id'])
        return predicate(*args)
    monkeypatch.setattr(retain, 'audited_rejection', classify)
    original = audit._unresolved_rows
    scans = []
    def scan(*args):
        scans.append(args[2])
        if len(scans) == (1 if phase == 'initial' else 2):
            ticks = [args[2], args[2] + audit.cut.MAX_AGE + 1]
            def clock():
                return ticks.pop(0) if len(ticks) > 1 else ticks[0]
            monkeypatch.setattr(audit.cut.time, 'monotonic', clock)
        return original(*args)
    monkeypatch.setattr(audit, '_unresolved_rows', scan)
    with pytest.raises(Refused, match='evidence_expired'):
        audit.report(live, shadow, hub, 'default')
    assert len(scans) == (1 if phase == 'initial' else 2)
    assert len(set(scans)) == 1
    assert len(classified) == (1 if phase == 'initial' else 26)


def test_unresolved_collector_stops_at_bounded_sentinel(case):
    (live, shadow), _hub, _proof, _calls = case
    for i in range(40):
        live.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                     (f'unknown-{i}', 'tasks', 't_card', 'upsert', '{}', 'rejected', 'unknown'))
    rows = audit._unresolved_rows(live, shadow, audit.time.monotonic())
    assert len(rows) == 21
    assert all(r['last_error'] in ('unknown', 'spend_decision_pending: stopped') for r in rows)
