"""Opt-in evidence uses the existing reserved report, never a second proof cut."""
import copy
import json
import sqlite3

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_refusal_audit as audit
from hermes_cli.kanban_history_residue_source import Refused
from tests.hermes_cli import test_kanban_sync_refusal_audit as helpers

case = helpers.case


def test_same_report_detached_exact_diff_inputs_without_second_queries(case):
    (live, shadow), hub, _proof, _calls = case
    expected = audit.report(live, shadow, hub, 'default')
    statements = []
    for conn in (live, shadow):
        conn.set_trace_callback(statements.append)
    evidence = {'stale': True}
    actual = audit.report(live, shadow, hub, 'default', evidence=evidence)
    assert actual == expected
    assert 'stale' not in evidence
    assert evidence['checked'] == {'t_card'}
    assert evidence['pending'] == set()
    assert evidence['mine'] == {'t_card'}
    assert evidence['tasks']['live'] == client._task_rows(live)
    assert evidence['tasks']['shadow'] == client._task_rows(shadow)
    assert evidence['state'] == {'board_id': 'default', 'gateway_id': 'home', 'cursor': 100}
    assert evidence['outbox_pending'] == 0
    assert evidence['rejected'] == [dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone())]
    assert evidence['audits'] == [{'op_id': 'rejected-op', 'ordinary': False, 'diagnostic': None, 'custody': None}]
    assert set(evidence['children']) == {'task_links', 'task_comments', 'task_runs', 'task_chat_links'}
    # Capture has no second task/child extraction. Each connection is read once.
    assert sum('FROM tasks t LEFT JOIN task_runs' in s for s in statements[:]) == 4  # 2 report + 2 expected helpers above
    old = copy.deepcopy(evidence)
    live.execute("UPDATE tasks SET title='later'")
    assert evidence == old


@pytest.mark.parametrize('fault', ['begin_head', 'end_head', 'next', 'more', 'nonempty', 'writer_live', 'writer_shadow'])
def test_failed_report_never_publishes_partial_or_stale_capture(case, fault):
    (live, shadow), hub, _proof, _calls = case
    evidence = {'previous': True}
    n = 0
    def changes(*_args):
        nonlocal n
        n += 1
        assert not live.in_transaction and not shadow.in_transaction
        result = dict(changes=[], more=False, head=100, next=100)
        if fault == 'begin_head' or (fault == 'end_head' and n == 2):
            result['head'] = result['next'] = 101
        if fault == 'next':
            result['next'] = 101
        if fault == 'more':
            result['more'] = True
        if fault == 'nonempty':
            result['changes'] = [{'seq': 101}]
        if fault.startswith('writer_') and n == 2:
            target = live if fault == 'writer_live' else shadow
            path = target.execute('PRAGMA database_list').fetchone()[2]
            with sqlite3.connect(path) as writer:
                writer.execute('CREATE TABLE unrelated(value TEXT)')
        return result
    hub.changes = changes
    with pytest.raises(Refused):
        audit.report(live, shadow, hub, 'default', evidence=evidence)
    assert evidence == {}
    assert not live.in_transaction and not shadow.in_transaction


def test_exact_child_pairs_and_pending_without_task_id(case):
    (live, shadow), hub, _proof, _calls = case
    for conn in (live, shadow):
        conn.execute("INSERT INTO task_comments VALUES ('t_card','comment')")
    shadow.execute("INSERT INTO task_comments VALUES ('t_card','other')")
    live.execute("INSERT INTO kanban_sync_outbox VALUES ('pending-null','task_events','event2','upsert','{}','pending',NULL)")
    evidence = {}
    report = audit.report(live, shadow, hub, 'default', evidence=evidence)
    assert report['pending'] == 0 and evidence['outbox_pending'] == 1
    assert report['child_mismatch'] == {'task_comments': 1}
    assert evidence['children']['task_comments'] == {'live': {('t_card', 'comment')}, 'shadow': {('t_card', 'comment'), ('t_card', 'other')}}


def test_capture_failure_not_published_and_reservations_exclude_writer(case, monkeypatch):
    from hermes_cli import kanban_sync_diff_evidence as export
    (live, shadow), hub, _proof, _calls = case
    original = export.finish
    def finish(*args):
        for conn in (live, shadow):
            assert conn.in_transaction
            path = conn.execute('PRAGMA database_list').fetchone()[2]
            writer = sqlite3.connect(path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    writer.execute('BEGIN IMMEDIATE')
            finally:
                writer.close()
        original(*args)
        raise ValueError('injected capture failure')
    monkeypatch.setattr(export, 'finish', finish)
    evidence = {'previous': True}
    with pytest.raises(ValueError, match='injected capture failure'):
        audit.report(live, shadow, hub, 'default', evidence=evidence)
    assert evidence == {}
    assert not live.in_transaction and not shadow.in_transaction


def test_fresh_diagnostic_not_inferred_from_ordinary_union(case, monkeypatch):
    from hermes_cli import kanban_sync_actionability_drift as diagnostic
    from hermes_cli import kanban_sync_retain_drift as retain
    (live, shadow), hub, _proof, _calls = case
    live.execute("UPDATE kanban_sync_outbox SET op='actionability_record', fields=?", (json.dumps({'task':'t_card'}),))
    monkeypatch.setattr(retain, 'audited_rejection', lambda *_a: True)
    calls = []
    def fresh(*_args, fresh_gateway=None):
        calls.append(fresh_gateway)
        return False
    monkeypatch.setattr(diagnostic, 'audited_rejection', fresh)
    evidence = {}
    report = audit.report(live, shadow, hub, 'default', evidence=evidence)
    assert report['clean'] and report['audited_retain_rejections'] == ['rejected-op']
    assert calls == ['home']
    assert evidence['audits'][0]['ordinary'] is True
    assert evidence['audits'][0]['diagnostic'] is False


def test_expiration_after_capture_does_not_publish(case, monkeypatch):
    from hermes_cli import kanban_sync_diff_evidence as export
    (live, shadow), hub, _proof, _calls = case
    original = export.finish
    def finish(*args):
        original(*args)
        monkeypatch.setattr(audit.cut.time, 'monotonic', lambda: 100000000000.0)
    monkeypatch.setattr(export, 'finish', finish)
    evidence = {'previous': True}
    with pytest.raises(Refused, match='expired'):
        audit.report(live, shadow, hub, 'default', evidence=evidence)
    assert evidence == {}


def test_retain_capture_includes_original_full_custody(case):
    from tests.hermes_cli.test_kanban_refusal_inventory import seed_retains
    (live, shadow), hub, _proof, _calls = case
    seed_retains(live, shadow)
    evidence = {}
    result = audit.report(live, shadow, hub, 'default', evidence=evidence)
    retains = [r for r in evidence['audits'] if r['op_id'] != 'rejected-op']
    assert len(retains) == result['rejected_ops'] - 1
    assert retains
    assert all(r['ordinary'] and r['diagnostic'] is None and r['custody']['delivery_state'] == 'rejected' for r in retains)
