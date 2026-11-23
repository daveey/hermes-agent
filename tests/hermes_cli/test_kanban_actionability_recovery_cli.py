"""Real parser/dispatch/producer path, isolated Board peer and custody database."""
import argparse
from contextlib import nullcontext
import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_actionability_recovery_cli as command
from tests.hermes_cli import test_kanban_actionability_recovery as support

live = support.live


def args(operation, path):
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    return parser.parse_args(['kanban', '--board', 'default', 'record-actionability',
                              '--operation', operation, '--request-file', str(path)])


def test_parser_dispatch_preview_apply_escalate_frozen_request(live, tmp_path, monkeypatch, capsys):
    tid, hub, obligation = support.setup(live)
    path = tmp_path / 'request.json'
    path.write_text(json.dumps({'task_id': tid, **obligation}))
    monkeypatch.setattr(command, '_connection', lambda: nullcontext(live))
    monkeypatch.setattr(command, '_hub', lambda: nullcontext(hub))
    monkeypatch.setattr(cli.kb, 'init_db', lambda: pytest.fail('recovery initialized board'))
    assert cli.kanban_command(args('preview', path)) == 0
    value = json.loads(capsys.readouterr().out)
    assert value['issued'] is False and not hub.sent
    path.write_text(json.dumps({'op_id': value['op_id'], 'request_sha256': value['request_sha256']}))
    assert cli.kanban_command(args('apply', path)) == 0
    assert json.loads(capsys.readouterr().out)['delivery_state'] == 'confirmed'
    hub.now = obligation['deadline_at']
    assert cli.kanban_command(args('escalate', path)) == 0
    assert json.loads(capsys.readouterr().out)['execution_authority'] is False
    assert hub.sent[0]['payload'] == hub.sent[-1]['payload']


@pytest.mark.parametrize('raw', ['[]', '{', '{}', '{"op_id":"x","request_sha256":"y","deadline_at":1}'])
def test_bad_confirmation_never_connects_or_sends(tmp_path, monkeypatch, capsys, raw):
    path = tmp_path / 'request.json'
    path.write_text(raw)
    monkeypatch.setattr(command, '_connection', lambda: pytest.fail('invalid request opened board'))
    assert cli.kanban_command(args('apply', path)) == 1
    assert 'record-actionability:' in capsys.readouterr().err


def test_cli_is_fenced_for_worker_descendants(tmp_path, monkeypatch):
    from agent import delegation_context
    path = tmp_path / 'request.json'
    path.write_text('{}')
    monkeypatch.setattr(delegation_context, 'kanban_path_is_fenced', lambda *a: True)
    monkeypatch.setattr(command, 'command', lambda *a: pytest.fail('fenced mutation reached handler'))
    assert cli.kanban_command(args('preview', path)) == 1
