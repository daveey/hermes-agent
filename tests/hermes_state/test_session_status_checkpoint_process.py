"""A process supervised by another backend must veto a completed session."""
import json

import pytest

from tools import process_registry as processes
from tests.hermes_state import test_session_status_refresh as fixtures

board = fixtures.board
receipts = fixtures.receipts
db = fixtures.db
turn = fixtures.turn
refresh = fixtures.refresh


@pytest.mark.parametrize('fate', ['running', 'gone'])
def test_foreign_checkpoint_process_vetoes_green(db, tmp_path, monkeypatch, fate):
    turn(db)
    checkpoint = tmp_path / 'processes.json'
    checkpoint.write_text(json.dumps([{
        'session_id': 'proc_foreign', 'parent_session_id': 'chat',
        'pid': 12345, 'host_start_time': 999, 'pid_scope': 'host',
        'command': 'sleep 600',
    }]))
    monkeypatch.setattr(processes, '_checkpoint_path', lambda: checkpoint)
    monkeypatch.setattr(processes.process_registry, '_detached_host_fate', lambda pid, birth: fate)
    value = refresh(db)
    assert value['kind'] != 'done', value
    assert value['presentation']['stripe'] != 'done', value
    if fate == 'running':
        assert value['kind'] == 'waiting'
        assert value['on'][0]['state'] == 'running'
        assert value['on'][0]['id'] == 'process:proc_foreign'


def test_foreign_checkpoint_other_owner_is_not_this_sessions_work(db, tmp_path, monkeypatch):
    turn(db)
    checkpoint = tmp_path / 'processes.json'
    checkpoint.write_text(json.dumps([{'session_id': 'proc_other', 'parent_session_id': 'other'}]))
    monkeypatch.setattr(processes, '_checkpoint_path', lambda: checkpoint)
    assert refresh(db)['kind'] == 'done'


def test_malformed_checkpoint_is_unknown_not_empty(db, tmp_path, monkeypatch):
    turn(db)
    checkpoint = tmp_path / 'processes.json'
    checkpoint.write_text('{}')
    monkeypatch.setattr(processes, '_checkpoint_path', lambda: checkpoint)
    value = refresh(db)
    assert value['presentation']['stripe'] != 'done'
    assert value['coverage']['state'] == 'unknown'
