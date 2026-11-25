"""Checkpoint liveness never overrides completion, owner or sandbox custody."""
import json

import pytest

from tests.hermes_state import test_session_status_checkpoint_process as fixtures
from tests.hermes_state import test_session_status_refresh as status
from tools import process_registry as processes

board = fixtures.board
receipts = fixtures.receipts
db = fixtures.db


def checkpoint(tmp_path, monkeypatch, entries):
    path = tmp_path / 'processes.json'
    path.write_text(json.dumps(entries))
    monkeypatch.setattr(processes, '_checkpoint_path', lambda: path)
    return path


def entry(**changes):
    return {'session_id': 'proc_example', 'parent_session_id': 'chat',
            'pid': 12345, 'host_start_time': 999, 'pid_scope': 'host',
            'command': 'sleep 600', **changes}


@pytest.mark.parametrize('change', [
    {'pid': True}, {'pid': 0}, {'host_start_time': True}, {'host_start_time': 0},
    {'session_id': None}, {'session_id': 'wrong'}, {'pid_scope': 'container'},
    {'parent_session_id': None},
])
def test_unverifiable_checkpoint_cannot_certify_green(db, tmp_path, monkeypatch, change):
    status.turn(db)
    checkpoint(tmp_path, monkeypatch, [entry(**change)])
    monkeypatch.setattr(processes.process_registry, '_detached_host_fate',
                        lambda *args: pytest.fail('invalid identity must not reach kernel probe'))
    value = status.refresh(db)
    assert value['kind'] != 'done'
    assert value['coverage']['state'] == 'unknown'


@pytest.mark.parametrize('code,expected', [(0, 'done'), (3, 'unresolved')])
def test_structured_receipt_outranks_stale_checkpoint(db, receipts, tmp_path, monkeypatch, code, expected):
    status.turn(db)
    status.receipt(receipts, exit_code=code)
    checkpoint(tmp_path, monkeypatch, [entry()])
    monkeypatch.setattr(processes.process_registry, '_detached_host_fate',
                        lambda *args: pytest.fail('completion is authoritative'))
    value = status.refresh(db)
    assert value['kind'] == expected
    assert value['coverage']['state'] == 'complete'


def test_absent_checkpoint_remains_known_empty(db, tmp_path, monkeypatch):
    status.turn(db)
    monkeypatch.setattr(processes, '_checkpoint_path', lambda: tmp_path / 'absent')
    assert status.refresh(db)['kind'] == 'done'


def test_unreadable_checkpoint_does_not_hide_retained_failure(db, receipts, tmp_path, monkeypatch):
    status.turn(db)
    status.receipt(receipts, exit_code=3)
    path = checkpoint(tmp_path, monkeypatch, [])
    path.write_text('{broken')
    value = status.refresh(db)
    assert value['kind'] != 'done'
    assert value['coverage']['state'] == 'unknown'
    # Even with an unavailable live inventory, loss of the retained failure must
    # not be interpreted as success. Recovery restores the structured evidence.
    path.write_text('[]')
    recovered = status.refresh(db)
    assert recovered['kind'] == 'unresolved'
    assert recovered['failures'][0]['id'] == 'process:proc_example'
