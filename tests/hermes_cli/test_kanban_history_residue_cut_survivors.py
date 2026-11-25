"""Unmask independent configured-board and fresh-cut contracts. [daveey]"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_history_residue_board as board
from hermes_cli import kanban_history_residue_cut as cut
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_cli as cli
from hermes_cli.kanban_history_residue_source import Refused
from tests.hermes_cli import test_kanban_history_residue_board as bf
from tests.hermes_cli import test_kanban_history_residue_cut as cf
from tests.hermes_cli import test_kanban_history_residue_cli as clf

stores = bf.stores
configured = bf.configured
cut_inputs = cf.cut_inputs


def test_read_does_not_create_custody(configured):
    path = board.ledger_path('default')
    assert not path.exists()
    with pytest.raises(sqlite3.OperationalError):
        board._ledger('default')
    assert not path.exists()


def test_paths_reject_swapped_handles(configured):
    paths, _, _, _, connect = configured
    with pytest.raises(Refused, match='configured_paths_required'):
        board._paths(connect(paths[1]), connect(paths[0]), 'default')


def test_paths_reject_identical_configured_databases(configured, monkeypatch):
    paths, _, _, _, connect = configured
    monkeypatch.setattr(client, 'shadow_path', lambda _: paths[0])
    with pytest.raises(Refused, match='configured_paths_required'):
        board._paths(connect(paths[0]), connect(paths[0]), 'default')


@pytest.mark.parametrize('reason', [None, 42, '', '  '])
def test_reason_refused_before_refresh(configured, reason):
    with pytest.raises(Refused, match='reason_required'):
        board.observe_board('default', 'op-original', reason)


@pytest.mark.parametrize('flag', ['busy', 'more'])
def test_observation_requires_complete_refresh(configured, monkeypatch, flag):
    _, _, calls, _, _ = configured
    monkeypatch.setattr(client, '_pull_replica', lambda *a, **kw: client.Pulled(**{flag: True}))
    with pytest.raises(Refused, match='diagnostic_refresh_incomplete'):
        board.observe_board('default', 'op-original', 'Keep history')
    assert not calls and not board.ledger_path('default').exists()


def test_existing_intent_uses_original_expected_version(configured):
    paths, _, calls, _, connect = configured
    board.observe_board('default', 'op-original', 'Keep history')
    for path in paths:
        connect(path).execute('UPDATE tasks SET sync_version=8')
    assert board.observe_board('default', 'op-original', 'Keep history')['recorded']
    assert [c[0] for c in calls] == ['POST', 'GET', 'GET']


@pytest.mark.parametrize('count', [0, 21])
def test_board_ledger_bound_before_cut(configured, monkeypatch, count):
    paths, hub, _, _, connect = configured
    board.observe_board('default', 'op-original', 'Keep history')
    monkeypatch.setattr(cut, 'MAX_OPS', 0 if count == 21 else 20)
    if not count:
        identity = board.transport.scope(hub, 'default')
        monkeypatch.setattr(board.transport, 'scope', lambda *a: (identity[0], 'other', identity[2]))
    with pytest.raises(Refused, match='cut_ops_bound'):
        board.audit_diff(connect(paths[0]), connect(paths[1]), hub, 'default', pulled=client.Pulled(), started=cut.time.monotonic())


@pytest.mark.parametrize('change', [None, {'changes': [1]}, {'more': True}, {'head': 100.0}, {'next': 100.0}])
def test_empty_feed_each_independent_condition(change):
    data = dict(changes=[], more=False, head=100, next=100)
    if change is None:
        data = []
    else:
        data.update(change)
    with pytest.raises(Refused, match='authoritative_feed_changed'):
        cut._empty(SimpleNamespace(changes=lambda *a: data), 'default', 100)


@pytest.mark.parametrize('ids', [[], ['op-original'] * 2, ['op-original'] * 21])
def test_cut_op_inventory_refused_before_lookup(cut_inputs, ids):
    live, shadow, ledger, hub, _, _ = cut_inputs
    with pytest.raises(Refused, match='cut_ops_bound'):
        cut.verify(live, shadow, ledger, hub, 'default', ids, started=cut.time.monotonic())


def test_transaction_trace_uses_reader_then_writer():
    conn = sqlite3.connect(':memory:', isolation_level=None)
    trace = []
    conn.set_trace_callback(trace.append)
    try:
        with cut._transaction(conn):
            pass
        with cut._transaction(conn, reserve=True):
            pass
        assert [s for s in trace if s.startswith('BEGIN')] == ['BEGIN', 'BEGIN IMMEDIATE']
    finally:
        conn.close()


@pytest.mark.parametrize('head', [True, -1])
def test_snapshot_cursor_boundary(cut_inputs, monkeypatch, head):
    original = cut._capture
    def capture(*a):
        result = original(*a)
        for snap in result:
            snap['state'][0]['cursor'] = head
        return result
    monkeypatch.setattr(cut, '_capture', capture)
    with pytest.raises(Refused, match='replica_cursor_changed'):
        cf.verify(cut_inputs)


@pytest.mark.parametrize('category', ['missing_in_shadow', 'extra_in_shadow', 'child_mismatch'])
def test_all_ordinary_drift_categories_keep_cut_dirty(cut_inputs, monkeypatch, category):
    from hermes_cli import kanban_sync_client
    original = kanban_sync_client.diff
    def diff(*a):
        result = original(*a)
        result[category] = ['unrelated'] if category != 'child_mismatch' else {'task_comments': 1}
        return result
    monkeypatch.setattr(kanban_sync_client, 'diff', diff)
    assert cf.verify(cut_inputs)['diff']['clean'] is False


def test_unsealed_refused_before_receipt_decode(cut_inputs, monkeypatch):
    original = cut.custody.lookup
    def lookup(*a):
        row = original(*a)
        row['receipt_json'] = None
        return row
    monkeypatch.setattr(cut.custody, 'lookup', lookup)
    with pytest.raises(Refused, match='sealed_custody_missing'):
        cf.verify(cut_inputs)


def test_missing_parent_key_refused_at_boundary(cut_inputs, monkeypatch):
    original = cut.validate_current
    def validate(*a, **kw):
        result = original(*a, **kw)
        del result['parent']['title']
        return result
    monkeypatch.setattr(cut, 'validate_current', validate)
    with pytest.raises(Refused, match='canonical_parent_changed'):
        cf.verify(cut_inputs)


def test_no_ledger_does_not_attempt_audit(configured, monkeypatch, capsys):
    calls = []
    def audit(*a, **kw):
        calls.append(True)
        raise Refused('unexpected_audit')
    monkeypatch.setattr(board, 'audit_diff', audit)
    cli.cmd_sync(clf.args('diff', '--json'))
    assert not calls
    assert 'history_audit_error' not in json.loads(capsys.readouterr().out)


def test_evidence_remains_explicit(configured, capsys):
    board.observe_board('default', 'op-original', 'Keep history')
    assert cli.cmd_sync(clf.args('diff', '--json')) == 0
    assert 'history_audit_evidence' not in json.loads(capsys.readouterr().out)
