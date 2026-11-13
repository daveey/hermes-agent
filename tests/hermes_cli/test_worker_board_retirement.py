"""Removal cannot strand account registrations or race final admission."""
import errno
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import worker_account_boards as inventory
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli.admission_fence import AdmissionBusy, admission_lock


@pytest.fixture
def registered(monkeypatch, tmp_path):
    root = tmp_path / 'hermes_test'
    root.mkdir(exist_ok=True)
    monkeypatch.setenv('HERMES_HOME', str(root))
    monkeypatch.delenv('HERMES_KANBAN_DB', raising=False)
    monkeypatch.delenv('HERMES_KANBAN_BOARD', raising=False)
    monkeypatch.setattr(capacity, 'policy_root', lambda: root)
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    kb.create_board('retiring')
    path = kb.board_dir('retiring') / 'kanban.db'
    with sqlite3.connect(path) as conn:
        inventory.register(conn)
    return root, path


@pytest.mark.parametrize('archive', [True, False])
def test_public_remove_retires_registration(registered, archive):
    root, path = registered
    assert path in inventory.paths()
    assert list((capacity.policy_root() / 'worker-resources' / 'boards').glob('*.json'))
    result = kb.remove_board('retiring', archive=archive)
    assert result['action'] == ('archived' if archive else 'deleted')
    assert not path.exists()
    assert path not in inventory.paths()
    assert not list((root / 'worker-resources' / 'boards').glob('*.json'))
    if archive:
        assert (Path(result['new_path']) / 'kanban.db').exists()


def test_running_row_preserves_board_and_pointer(registered):
    _, path = registered
    with sqlite3.connect(path) as conn:
        # Use the full production schema's defaults, no actual worker spawned.
        conn.execute("INSERT INTO tasks (id, title, status, assignee, created_at) "
                     "VALUES ('t_busy', 'Busy', 'running', 'default', 1)")
    with pytest.raises(ValueError, match='running cards'):
        kb.remove_board('retiring')
    assert path.exists()
    assert path in inventory.paths()
    assert list((capacity.policy_root() / 'worker-resources' / 'boards').glob('*.json'))


def test_inflight_writer_refuses_removal(registered):
    _, path = registered
    conn = sqlite3.connect(path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='locked'):
            kb.remove_board('retiring')
        assert path in inventory.paths()
        assert list((capacity.policy_root() / 'worker-resources' / 'boards').glob('*.json'))
    finally:
        conn.rollback()
        conn.close()


def test_removal_retains_account_lock_until_files_gone(registered, monkeypatch):
    root, path = registered
    from hermes_cli import kanban_db_boards as boards
    original = boards._remove_board_files_locked

    def remove(*args, **kwargs):
        from hermes_cli.kanban_admission import claim_transaction
        conn = sqlite3.connect(path)
        try:
            with claim_transaction(conn) as admitted:
                assert admitted is False
        finally:
            conn.close()
        with pytest.raises(AdmissionBusy):
            with admission_lock(root, timeout=0):
                pytest.fail('concurrent final claim acquired account fence')
        return original(*args, **kwargs)

    monkeypatch.setattr(boards, '_remove_board_files_locked', remove)
    kb.remove_board('retiring')
    with admission_lock(root, timeout=0):
        assert not path.exists()


def test_failed_removal_keeps_registration(registered, monkeypatch):
    _, path = registered
    from hermes_cli import kanban_db_boards as boards

    def fail(*args, **kwargs):
        raise OSError('fixture removal failed')

    monkeypatch.setattr(boards, '_remove_board_files_locked', fail)
    with pytest.raises(OSError, match='fixture removal failed'):
        kb.remove_board('retiring')
    assert path in inventory.paths()
    assert list((capacity.policy_root() / 'worker-resources' / 'boards').glob('*.json'))


def test_post_rename_failure_retires_proven_idle_record(registered, monkeypatch):
    root, path = registered
    from hermes_cli import kanban_db_boards as boards

    def fail(*args, **kwargs):
        raise OSError('tombstone write failed')

    monkeypatch.setattr(boards, '_write_board_metadata_locked', fail)
    with pytest.raises(OSError, match='tombstone'):
        kb.remove_board('retiring')
    assert not path.exists()
    assert not list((root / 'worker-resources' / 'boards').glob('*.json'))
    inventory.paths()  # must not poison all account claims


def test_unreadable_board_after_failed_removal_keeps_registration(registered, monkeypatch):
    """removing()'s finally: a stat that fails with anything but ENOENT is not
    proof of removal (``except OSError: removed = False``), so the record stays
    and the body's own error, not the PermissionError, propagates."""
    root, path = registered
    blocked = os.fspath(path.resolve())
    real_stat = os.stat

    def denied_stat(target, *args, **kwargs):
        if isinstance(target, (str, os.PathLike)) and os.fspath(target) == blocked:
            raise PermissionError(errno.EACCES, 'Permission denied', blocked)
        return real_stat(target, *args, **kwargs)

    with pytest.raises(RuntimeError, match='fixture removal failed'):
        with monkeypatch.context() as denied, inventory.removing(path):
            path.unlink()  # absent underneath: only the EACCES hides it
            denied.setattr(os, 'stat', denied_stat)
            raise RuntimeError('fixture removal failed')
    assert list((root / 'worker-resources' / 'boards').glob('*.json'))


def test_enabled_metadata_only_board_can_be_removed(registered):
    root, path = registered
    # An unregistered board with metadata but no DB has no rows to retire.
    for record in (root / 'worker-resources' / 'boards').glob('*.json'):
        record.unlink()
    path.unlink()
    assert kb.remove_board('retiring')['action'] == 'archived'


def test_account_and_installation_fences_both_held(registered, monkeypatch, tmp_path):
    root, path = registered
    import hermes_constants
    other = tmp_path / 'other_installation'
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: other)
    with inventory.removing(path):
        for home in (root, other):
            with pytest.raises(AdmissionBusy):
                with admission_lock(home, timeout=0):
                    pytest.fail('missing fence')


def test_missing_registered_database_never_retires(registered):
    root, path = registered
    path.unlink()
    with pytest.raises(ValueError):
        kb.remove_board('retiring')
    assert list((root / 'worker-resources' / 'boards').glob('*.json'))
    with pytest.raises(FileNotFoundError):
        inventory.paths()
