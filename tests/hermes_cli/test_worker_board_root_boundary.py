"""Standalone proof homes must not enter the real account inventory."""
import hashlib
import json
import sqlite3

import pytest

import hermes_constants
import hermes_state_guard
from hermes_cli import worker_account_boards as inventory
from hermes_cli import worker_compute_capacity as capacity


@pytest.fixture
def account(monkeypatch, tmp_path):
    root = tmp_path / '.hermes'
    root.mkdir()
    monkeypatch.setattr(hermes_constants, '_account_home', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'policy_root', lambda: root)
    monkeypatch.setattr(hermes_state_guard, '_in_test_context', lambda: False)
    return root


@pytest.mark.parametrize('relative', [
    'cache/scratch/proof/kanban.db', 'test-home/kanban.db',
    '../foreign/.hermes/kanban.db', 'kanban/boards/valid/nested/kanban.db',
])
def test_standalone_foreign_home_never_registers(account, monkeypatch, relative):
    board = account / relative
    board.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('HERMES_HOME', str(board.parent))
    with sqlite3.connect(board) as conn:
        inventory.register(conn)
    assert not (account / 'worker-resources').exists()
    assert inventory.paths() == []


@pytest.mark.parametrize('relative', [
    'kanban.db', 'kanban/boards/work/kanban.db',
    'profiles/research/kanban.db', 'profiles/research/kanban/boards/work/kanban.db',
])
def test_real_default_named_and_profile_boards_register(account, relative):
    board = account / relative
    board.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(board) as conn:
        inventory.register(conn)
    records = list((account / 'worker-resources/boards').glob('*.json'))
    assert len(records) == 1
    assert board in inventory.paths()


def test_legacy_foreign_record_cannot_reenter_census(account):
    board = account / 'cache/scratch/old/kanban.db'
    board.parent.mkdir(parents=True)
    board.touch()
    records = account / 'worker-resources/boards'
    records.mkdir(parents=True)
    record = records / (hashlib.sha256(str(board).encode()).hexdigest() + '.json')
    record.write_text(json.dumps(str(board)))
    assert inventory.paths() == []
    assert record.exists(), 'census remains read-only'


def test_named_board_symlink_cannot_import_scratch(account):
    scratch = account / 'cache/scratch/proof'
    scratch.mkdir(parents=True)
    (scratch / 'kanban.db').touch()
    boards = account / 'kanban/boards'
    boards.mkdir(parents=True)
    (boards / 'work').symlink_to(scratch, target_is_directory=True)
    assert inventory.paths() == []
