"""Registered installation boards remain visible across dispatcher roots."""
import sqlite3

import pytest

from hermes_cli import worker_account_boards as inventory
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli import machine_metrics, kanban_db as kb


def test_registered_foreign_board_counts_after_installation_changes(monkeypatch, tmp_path):
    account = tmp_path / 'account'
    account.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    foreign = tmp_path / 'foreign' / 'kanban.db'
    foreign.parent.mkdir()
    conn = sqlite3.connect(foreign)
    conn.execute('CREATE TABLE tasks (id TEXT, status TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('t_foreign', 'running')")
    conn.commit()
    try:
        inventory.register(conn)
        inventory.register(conn)
        monkeypatch.setattr(kb, 'kanban_home', lambda: account)
        monkeypatch.setattr('hermes_cli.kanban_db_boards.named_board_db_files', list)
        owners = set()
        assert machine_metrics.active_card_runs(extra_paths=inventory.paths(), owner_output=owners) == 1
        assert owners == {(str(foreign.resolve()), 't_foreign')}
        assert machine_metrics.active_card_runs(extra_paths=[foreign, foreign]) == 1
    finally:
        conn.close()
    foreign.unlink()
    with pytest.raises(FileNotFoundError):
        inventory.paths()


@pytest.mark.parametrize('raw', ['{broken', '7', '"relative.db"'])
def test_corrupt_inventory_fails_closed(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    records = tmp_path / 'worker-resources' / 'boards'
    records.mkdir(parents=True)
    (records / 'bad.json').write_text(raw)
    with pytest.raises(ValueError):
        inventory.paths()


def test_register_directory_fsync_failure_propagates(monkeypatch, tmp_path):
    import os
    import stat
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    real_fsync = os.fsync
    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('directory fsync failed')
        real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    conn = sqlite3.connect(tmp_path / 'board.db')
    try:
        with pytest.raises(OSError, match='directory fsync failed'):
            inventory.register(conn)
    finally:
        conn.close()


def test_unregistered_account_boards_count(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    default = tmp_path / 'kanban.db'
    default.touch()
    named = tmp_path / 'kanban' / 'boards' / 'other' / 'kanban.db'
    named.parent.mkdir(parents=True)
    named.touch()
    assert set(inventory.paths()) == {default, named}


def _registered(conn_path):
    conn = sqlite3.connect(conn_path)
    try:
        inventory.register(conn)
    finally:
        conn.close()


def test_garbage_collected_scratch_board_retires_its_record(monkeypatch, tmp_path):
    """A scratch HERMES_HOME's board whose workspace was pruned no longer holds every claim."""
    import shutil
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    workspace = tmp_path / 'cache' / 'scratch' / 't_bench-x' / 'home'
    workspace.mkdir(parents=True)
    _registered(workspace / 'kanban.db')
    records = list((tmp_path / 'worker-resources' / 'boards').glob('*.json'))
    assert len(records) == 1 and inventory.paths() == [(workspace / 'kanban.db').resolve()]
    shutil.rmtree(tmp_path / 'cache' / 'scratch' / 't_bench-x')
    assert inventory.paths() == []
    assert not records[0].exists()


def test_missing_board_stays_unknown_unless_its_scratch_dir_is_gone(monkeypatch, tmp_path):
    """Fail closed for real boards: a non-ephemeral root, or a surviving scratch dir."""
    import shutil
    account = tmp_path / 'account'
    account.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    real = tmp_path / 'other-install' / 'kanban.db'
    real.parent.mkdir()
    _registered(real)
    shutil.rmtree(real.parent)
    with pytest.raises(FileNotFoundError):
        inventory.paths()
    shutil.rmtree(account / 'worker-resources')
    scratch = account / 'cache' / 'scratch' / 'run' / 'kanban.db'
    scratch.parent.mkdir(parents=True)
    _registered(scratch)
    scratch.unlink()  # the directory survives: a replaced or moving board, not a pruned workspace
    with pytest.raises(FileNotFoundError):
        inventory.paths()


def test_ephemeral_roots_never_cover_the_account(monkeypatch, tmp_path):
    import tempfile
    account = tmp_path / 'home' / '.hermes'
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    monkeypatch.setattr(tempfile, 'gettempdir', lambda: str(tmp_path / 'home'))
    roots = inventory._ephemeral_roots()
    assert (account / 'cache' / 'scratch').resolve() in roots
    assert not any(account.resolve().is_relative_to(root) for root in roots)


def test_pytest_never_registers_in_the_real_account_inventory(monkeypatch, tmp_path):
    """monkeypatch.undo() drops conftest's policy_root isolation; register must still refuse."""
    import hermes_constants
    monkeypatch.setattr(hermes_constants, '_account_home', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path / '.hermes')
    conn = sqlite3.connect(tmp_path / 'board.db')
    try:
        with pytest.raises(RuntimeError, match='real account worker inventory'):
            inventory.register(conn)
        monkeypatch.delenv('PYTEST_CURRENT_TEST')
        inventory.register(conn)
    finally:
        conn.close()
    assert inventory.paths() == [(tmp_path / 'board.db').resolve()]
