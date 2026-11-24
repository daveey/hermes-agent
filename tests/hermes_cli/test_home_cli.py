"""Explicit maintenance CLI routes to the authenticated owner, never raw row edits. [daveey]"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_home_cli as cli

_ARGS = ['--task','t_1234567890abcdef','--source','worker-a3','--nonce','fixture-nonce-00001','--expected-version','7']


@pytest.mark.parametrize('marker', ['HERMES_KANBAN_TASK','HERMES_KANBAN_WORKER_TASK'])
def test_worker_cannot_use_admin_move_cli(monkeypatch, marker):
    monkeypatch.setenv(marker, 't_1234567890abcdef')
    monkeypatch.setattr(cli, 'load_identity', lambda: pytest.fail('operator check must precede credentials'))
    with pytest.raises(PermissionError, match='owner operator surface'):
        cli.command(['source', *_ARGS])


@pytest.mark.parametrize(('step','method'), [('source','source_step'), ('destination','destination_step'),
                                           ('reconcile-precommit','reconcile_precommit')])
def test_explicit_cli_owner_route_and_bounded_receipt(monkeypatch, capsys, step, method):
    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_WORKER_TASK', raising=False)
    closed = []
    conn = SimpleNamespace(close=lambda: closed.append('db'))
    hub = SimpleNamespace(close=lambda: closed.append('hub'))
    monkeypatch.setattr(cli, 'Hub', lambda ident: hub)
    monkeypatch.setattr(cli, 'load_identity', lambda: object())
    monkeypatch.setattr(cli.dbc, 'connect', lambda: conn)
    def apply(c,h,b,i):
        assert (c,h,b) == (conn,hub,'default')
        assert i == {'task':'t_1234567890abcdef','source':'worker-a3','destination':'hub',
                     'nonce':'fixture-nonce-00001','expected_version':7}
        return {**i,'phase':'aborted','rows':[{'private':'never print manifest'}]}
    monkeypatch.setattr(cli.owner, method, apply)
    assert cli.command([step, *_ARGS]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['phase'] == 'aborted' and result['row_count'] == 1 and 'rows' not in result
    assert closed == ['db','hub']
