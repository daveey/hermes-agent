"""Exercise actual parser/CLI paths: a persisted receipt alone never makes a diff clean."""
from contextlib import contextmanager
import argparse
import json

import pytest

from hermes_cli import kanban_parser, kanban_sync_cli as cli
from hermes_cli import kanban_history_residue_board as board
from tests.hermes_cli import test_kanban_history_residue_board as fixtures

configured = fixtures.configured
stores = fixtures.stores


def args(*tail):
    parser = argparse.ArgumentParser()
    kanban_parser.build_parser(parser.add_subparsers())
    return parser.parse_args(['kanban', '--board', 'default', 'sync', *tail])


def test_real_parser_observe_then_cut_diff(configured, capsys):
    assert cli.cmd_sync(args('history-residue','op-original','--reason','Keep history','--json')) == 0
    assert json.loads(capsys.readouterr().out)['current_authority_verified'] is False
    assert cli.cmd_sync(args('diff','--json')) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['clean'] is True and report['rejected_ops'] == 1
    assert report['audited_history_rejections'] == ['op-original']
    assert report['history_audit_head'] == 100


@pytest.mark.parametrize('fault', ['busy','more','offline','stale','wrong-parent'])
def test_cli_cannot_hide_refresh_failure_with_saved_custody(configured, capsys, monkeypatch, fault):
    _paths, _hub, _calls, state, _connect = configured
    board.observe_board('default','op-original','Keep history')
    if fault in ('busy','more','offline'):
        from hermes_cli import kanban_sync_diagnostic as diagnostic
        @contextmanager
        def refresh_fault(*a, **kw):
            if fault == 'offline':
                raise OSError('offline fixture')
            yield cli.ksc.Pulled(**{fault: True})
        monkeypatch.setattr(diagnostic, 'fresh_cut', refresh_fault)
    if fault == 'stale': state['proof']['current']['observed_head'] = 99
    if fault == 'wrong-parent': state['proof']['current']['parent']['title'] = 'other'
    assert cli.cmd_sync(args('diff','--json')) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['clean'] is False and report['rejected_ops'] == 1
    assert not report.get('audited_history_rejections')


def test_cli_observation_is_explicit_and_reason_is_not_replaceable(configured, capsys):
    assert cli.cmd_sync(args('history-residue','op-original','--reason','Keep history','--json')) == 0
    capsys.readouterr()
    assert cli.cmd_sync(args('history-residue','op-original','--reason','different','--json')) == 1
    assert json.loads(capsys.readouterr().out) == {'recorded': False, 'reason': 'intent_conflict'}


def test_explicit_evidence_flag_keeps_exact_current_per_row_proof(configured, capsys):
    board.observe_board('default','op-original','Keep history')
    assert cli.cmd_sync(args('diff','--history-evidence','--json')) == 0
    audit = json.loads(capsys.readouterr().out)['history_audit_evidence']
    assert audit['begin']['head'] == audit['end']['head'] == audit['head'] == 100
    assert audit['evidence'][0]['op_id'] == 'op-original'
    assert audit['execution_authority'] is False
    assert json.loads(audit['evidence'][0]['proof_json'])['receipt']['source_json'] == audit['evidence'][0]['source_json']


@pytest.mark.parametrize('fault', [None, 'field', 'unproven', 'race'])
def test_mixed_history_and_server_audits_share_a_fresh_unchanged_cut(configured, capsys, monkeypatch, fault):
    from hermes_cli import kanban_sync_refusal_audit as refusal
    paths, _hub, _calls, _state, connect = configured
    board.observe_board('default', 'op-original', 'Keep history')
    live = connect(paths[0])
    row = dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE op_id='op-original'").fetchone())
    row['op_id'] = 'server-op'
    row['row_uid'] = 'server-event'
    live.execute(f"INSERT INTO kanban_sync_outbox ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                 tuple(row.values()))
    reason, _, detail = row['last_error'].partition(': ')
    proof = dict(version=1, board='default', gateway='home', execution_authority=False,
                 historical_identity_certified=False, receipt=dict(client_op_id='server-op',
                 status='rejected', reason=reason, detail=detail or None))
    def receipt(*unused):
        if fault == 'race':
            other = connect(paths[0])
            other.execute("UPDATE kanban_sync_outbox SET attempts=attempts+1 WHERE op_id='server-op'")
        return None if fault == 'unproven' else proof
    monkeypatch.setattr(refusal, '_receipt', receipt)
    if fault == 'field':
        live.execute("UPDATE tasks SET title='real drift'")
    before = list(live.iterdump())
    assert cli.cmd_sync(args('diff', '--json')) == (0 if fault is None else 1)
    result = json.loads(capsys.readouterr().out)
    if fault is None:
        assert result['audited_server_refusals'] == ['server-op']
        assert result['audited_history_rejections'] == ['op-original']
        assert result['server_refusal_evidence']['head'] == result['history_audit_head'] == 100
        assert list(live.iterdump()) == before
    else:
        assert result['clean'] is False


def test_known_refusal_bypasses_unconfirmed_history_intent_without_changing_it(configured, capsys, monkeypatch):
    from hermes_cli import kanban_sync_refusal_audit as refusal
    from hermes_cli.kanban_sync_client import HubError
    _paths, _hub, calls, _state, connect = configured
    original = board.transport._request
    def failed_post(hub, method, path, body):
        if method == 'POST':
            calls.append((method,path,body))
            raise HubError(409, 'history_residue_op_already_known')
        return original(hub, method, path, body)
    monkeypatch.setattr(board.transport, '_request', failed_post)
    with pytest.raises(HubError, match='history_residue_op_already_known'):
        board.observe_board('default','op-original','Keep history')
    with board.closing(board._ledger('default')) as ledger:
        before = list(ledger.iterdump())
    live = connect(_paths[0])
    row = dict(live.execute("SELECT * FROM kanban_sync_outbox WHERE op_id='op-original'").fetchone())
    reason, _, detail = row['last_error'].partition(': ')
    proof = dict(version=1, board='default', gateway='home', execution_authority=False,
                 historical_identity_certified=False, receipt=dict(client_op_id='op-original',
                 status='rejected',reason=reason,detail=detail or None))
    monkeypatch.setattr(refusal, '_receipt', lambda *a: proof)
    assert cli.cmd_sync(args('diff','--json')) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['clean'] and result['audited_server_refusals'] == ['op-original']
    assert not result.get('history_audit_error')
    assert [c[0] for c in calls] == ['POST']
    with board.closing(board._ledger('default')) as ledger:
        assert list(ledger.iterdump()) == before
