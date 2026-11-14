"""Standalone proof homes must not enter the real account inventory."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

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


@pytest.mark.parametrize('missing', [False, True])
def test_legacy_foreign_record_cannot_reenter_census(account, missing):
    board = account / 'cache/scratch/old/kanban.db'
    board.parent.mkdir(parents=True)
    board.touch()
    records = account / 'worker-resources/boards'
    records.mkdir(parents=True)
    record = records / (hashlib.sha256(str(board).encode()).hexdigest() + '.json')
    record.write_text(json.dumps(str(board)))
    if missing:
        board.unlink()
    assert inventory.paths() == []
    assert record.exists(), 'census remains read-only'


def _running_board(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks (id TEXT, status TEXT)')
        conn.execute("INSERT INTO tasks VALUES ('real-run', 'running')")


@pytest.mark.parametrize('malformed', [False, True])
def test_host_census_ignores_current_scratch_home(account, monkeypatch, malformed):
    from hermes_cli import kanban_db as kb, worker_resource_admission as admission
    real = account / 'kanban.db'
    _running_board(real)
    scratch = account / 'cache/scratch/current/kanban.db'
    _running_board(scratch)
    if malformed:
        scratch.write_text('not sqlite')
    monkeypatch.setattr(kb, 'kanban_home', lambda: scratch.parent)
    monkeypatch.setattr(admission, 'pool_occupancy', lambda pool, owners, active, **kw: set(active))
    assert admission.occupied_count_for_host() == 1


def test_host_census_ignores_named_symlink(account, monkeypatch):
    from hermes_cli import kanban_db as kb, worker_resource_admission as admission
    _running_board(account / 'kanban.db')
    scratch = account / 'cache/scratch/link/kanban.db'
    _running_board(scratch)
    boards = account / 'kanban/boards'
    boards.mkdir(parents=True)
    (boards / 'work').symlink_to(scratch.parent, target_is_directory=True)
    monkeypatch.setattr(kb, 'kanban_home', lambda: account)
    monkeypatch.setattr(admission, 'pool_occupancy', lambda pool, owners, active, **kw: set(active))
    assert admission.occupied_count_for_host() == 1


@pytest.mark.parametrize('relative', ['profiles/research/kanban.db',
                                    'profiles/research/kanban/boards/work/kanban.db'])
def test_unregistered_profile_discovered(account, relative):
    board = account / relative
    _running_board(board)
    assert board in inventory.paths()


@pytest.mark.parametrize('error', [PermissionError, OSError])
def test_unreadable_profile_remains_unknown(account, monkeypatch, error):
    from hermes_cli import worker_resource_admission as admission
    profile = account / 'profiles/research'
    _running_board(profile / 'kanban.db')
    original = os.stat

    def denied(path, *args, **kwargs):
        if Path(path) == profile:
            raise error('profile stat unavailable')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', denied)
    with pytest.raises(error):
        inventory.paths()
    with pytest.raises(error):
        admission.occupied_count_for_host()


def test_profile_non_directory_or_vanished_entry_is_skipped(account, monkeypatch):
    profiles = account / 'profiles'
    profiles.mkdir()
    (profiles / 'readme').touch()
    vanished = profiles / 'vanished'
    original = Path.iterdir
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter([profiles / 'readme', vanished])
                        if path == profiles else original(path))
    assert inventory.paths() == []


def test_registered_missing_real_board_remains_unknown(account):
    board = account / 'kanban.db'
    with sqlite3.connect(board) as conn:
        inventory.register(conn)
    board.unlink()
    with pytest.raises(FileNotFoundError):
        inventory.paths()


def test_authoritative_empty_and_disappearing_inventory(account, monkeypatch):
    from hermes_cli import kanban_db as kb, machine_metrics
    board = account / 'kanban.db'
    _running_board(board)
    monkeypatch.setattr(kb, 'kanban_home', lambda: account)
    assert machine_metrics.active_card_runs(board_paths=[]) == 0
    board.unlink()
    assert machine_metrics.active_card_runs(board_paths=[board]) is None


def test_named_board_symlink_cannot_import_scratch(account):
    scratch = account / 'cache/scratch/proof'
    scratch.mkdir(parents=True)
    (scratch / 'kanban.db').touch()
    boards = account / 'kanban/boards'
    boards.mkdir(parents=True)
    (boards / 'work').symlink_to(scratch, target_is_directory=True)
    assert inventory.paths() == []
