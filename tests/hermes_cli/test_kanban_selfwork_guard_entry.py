"""Authenticated CLI and tool intake both enforce the authority's freeze."""
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import config
from hermes_cli.kanban_selfwork_receiver import receive
from tests.hermes_cli import test_kanban_selfwork_nochat as support
from tests.hermes_cli import test_kanban_selfwork_guards as guards

world = support.world
request_envelope = support.request_envelope


@pytest.mark.parametrize('surface', ['cli', 'tool'])
@pytest.mark.parametrize('mode', ['refuse', 'append', 'off', 'non-guard'])
def test_guard_policy_at_entry(request_envelope, monkeypatch, capsys, surface, mode, caplog):
    original = config.load_config
    def configured(*args, **kwargs):
        value = original(*args, **kwargs)
        value['kanban']['self_work']['freeze_guards'] = mode != 'off'
        return value
    monkeypatch.setattr(config, 'load_config', configured)
    request_envelope['args']['create']['self_work']['kind'] = 'sweep' if mode == 'non-guard' else 'guard'
    with kbc.connect_closing() as conn:
        tid = guards.seed(conn) if mode == 'append' else None
    with caplog.at_level('INFO'):
        result = (support.invoke(request_envelope, monkeypatch, capsys) if surface == 'cli'
                  else receive(request_envelope))
    with kbc.connect_closing() as conn:
        if mode == 'refuse':
            assert result['status'] == 'rejected', result
            assert not kb.list_tasks(conn)
        elif mode == 'append':
            assert result['disposition'] == 'append', result
            assert result['task_id'] == tid
            assert len(kb.list_comments(conn, tid)) == 1
            assert len(kb.list_tasks(conn)) == 1
        else:
            assert result.get('created') is True, result
    assert len([r for r in caplog.records if 'guard creation frozen' in r.message]) == (mode in {'refuse', 'append'})
