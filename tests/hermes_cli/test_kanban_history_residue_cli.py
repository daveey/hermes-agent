"""Exercise actual parser/CLI paths: a persisted receipt alone never makes a diff clean."""
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
    if fault in ('busy','more'):
        monkeypatch.setattr(cli.ksc, '_pull_replica', lambda *a, **k: cli.ksc.Pulled(**{fault: True}))
    if fault == 'offline':
        def offline(*a, **kw):
            raise OSError('offline fixture')
        monkeypatch.setattr(cli.ksc, '_pull_replica', offline)
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
