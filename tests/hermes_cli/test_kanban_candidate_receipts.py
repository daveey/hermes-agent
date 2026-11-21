"""Per-pass refusal evidence, including the two Phase A UNKNOWN shapes."""
import sqlite3
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_candidate_receipts as receipts
from hermes_cli import kanban_worker_node as node


def connection():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    return conn


def test_replay_unknown_shapes(monkeypatch):
    from hermes_cli import kanban_runs_on_capabilities as capabilities
    from hermes_cli import kanban_workspace_source as source
    conn = connection()
    lookup = Mock()
    lookup.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(node, 'eligible', lambda card, *args: 'labels_mismatch' if card['id'] == 'mac' else None)
    monkeypatch.setattr(capabilities, 'claim_refusal', lambda *args, **kwargs: None)
    monkeypatch.setattr(source, 'pickup_skip', lambda *args: None)
    with receipts.collect(worker='worker-a1', now_ms=123000) as capture:
        for tid in ('mac', 'coach'):
            card = dict(id=tid, status='ready', runs_on=['os:macos'] if tid == 'mac' else ['pool:worker'], assignee='default')
            receipts.scores([(card, Mock(total=125.0))])
            reason = node._skip_reason(lookup, card, 'worker-a1', {}, mode='pool', review_enabled=True,
                                       profile_exists=lambda _: False)
            assert reason is not None
    receipts.persist(conn, capture)
    rows = receipts.read(conn)['receipts']
    assert {r['task_id']: r['gate'] for r in rows} == {'mac': 'labels', 'coach': 'prefilter'}
    assert all(r['score'] == 125.0 and r['worker'] == 'worker-a1' and r['pass_time_ms'] == 123000 for r in rows)
    assert len({r['pass_id'] for r in rows}) == 1


def test_first_gate_score_and_claim_suppression():
    conn = connection()
    with receipts.collect(worker='a2', now_ms=1000) as capture:
        receipts.note('one', 'prefilter', 'profile missing', stage='pickup')
        receipts.note('one', 'budget/pacing', 'later hold', stage='claim')
        receipts.note('claimed', 'capacity', 'old refusal', stage='pickup')
        receipts.claimed('claimed')
    receipts.persist(conn, capture)
    row, = receipts.read(conn)['receipts']
    assert row['gate'] == 'prefilter' and row['stage'] == 'pickup'
    assert row['score'] is None and row['score_status'] == 'not_observed'


def test_retention_row_age_and_pass_bounds(monkeypatch):
    conn = connection()
    monkeypatch.setattr(receipts, 'MAX_ROWS', 3)
    monkeypatch.setattr(receipts, 'MAX_PASSES', 2)
    for now in (1000, 2000, 3000):
        with receipts.collect(worker='a1', now_ms=now, limit=2) as capture:
            for tid in ('a', 'b', 'c'):
                receipts.note(tid, 'capacity', 'full', stage='pickup')
        receipts.persist(conn, capture)
    result = receipts.read(conn)
    assert len(result['receipts']) == 3 and len(result['passes']) == 2
    assert all(p['dropped'] == 1 for p in result['passes'])
    with receipts.collect(worker='a1', now_ms=receipts.RETENTION_MS + 4000) as capture:
        pass
    receipts.persist(conn, capture)
    assert receipts.read(conn)['receipts'] == []
    assert len(receipts.read(conn)['passes']) == 1


def test_reader_empty_and_filtered_and_dry_scope():
    conn = connection()
    assert receipts.read(conn) == {'version': 1, 'receipts': [], 'passes': []}
    receipts.note('outside', 'labels', 'not active', stage='pickup')
    with receipts.collect(worker='a1', now_ms=1000) as outer:
        receipts.note('outer', 'labels', 'x', stage='pickup')
        with receipts.collect(worker='a2', now_ms=2000) as inner:
            receipts.note('inner', 'capability', 'y', stage='pickup')
    receipts.persist(conn, outer)
    receipts.persist(conn, inner)
    assert [r['task_id'] for r in receipts.read(conn, task_id='inner')['receipts']] == ['inner']
    assert len(receipts.read(conn)['receipts']) == 2


@pytest.mark.parametrize('fact,value,gate', [('default_placement', True, 'prefilter'),
    ('placement_refusal', 'labels_mismatch', 'labels'), ('capability_refusal', 'missing gpu', 'capability'),
    ('source_refusal', 'source custody', 'provenance')])
def test_gate_facts_classification(fact, value, gate):
    with receipts.collect(worker='a1', now_ms=1000) as capture:
        receipts.pickup_facts('x', {fact: value}, status='ready')
    assert capture.records['x']['gate'] == gate


@pytest.mark.parametrize('code,detail,gate', [('quota_hold', 'quota', 'budget/pacing'),
    ('other', 'llm_paced: reserve', 'budget/pacing'), ('budget_exhausted', 'full', 'capacity'),
    ('hold', 'held', 'capacity'), ('other', 'containment uncertified', 'containment'),
    ('guard_held', 'cooldown', 'prefilter'), ('other', 'import failed', 'prefilter')])
def test_pickup_later_gate(code, detail, gate):
    with receipts.collect(worker='a1') as capture:
        receipts.pickup_skip('x', (code, detail))
    assert capture.records['x']['gate'] == gate


def test_suppression_and_unknown_are_not_invented_decisions():
    from hermes_cli.kanban_db_dispatch import DispatchResult
    with receipts.collect(worker='a1') as capture:
        receipts.pickup_skip('local', ('', None))
        receipts.pickup_facts('local', {'already_local': True}, status='ready')
        receipts.lane_refused('unclassified', DispatchResult(), status='ready')
        receipts.pickup_facts('held', {'placement_refusal': 'held'}, status='ready')
    assert 'local' not in capture.records
    assert capture.records['unclassified']['gate'] == 'unknown'
    assert capture.records['held']['gate'] == 'capacity'


def test_cli_readonly_export(tmp_path, monkeypatch, capsys):
    import json
    import sys
    path = tmp_path / 'evidence.db'
    conn = sqlite3.connect(path)
    with receipts.collect(worker='a1', now_ms=1000) as capture:
        receipts.note('wanted', 'labels', 'mismatch', stage='pickup')
    receipts.persist(conn, capture)
    conn.close()
    before = path.read_bytes()
    monkeypatch.setattr(sys, 'argv', ['receipts', '--db', str(path), '--task-id', 'wanted'])
    receipts.main()
    out = json.loads(capsys.readouterr().out)
    assert out['receipts'][0]['task_id'] == 'wanted'
    assert path.read_bytes() == before
