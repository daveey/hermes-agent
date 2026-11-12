"""Interrupted retirement keeps a durable proof, not an absence heuristic."""
import json
import sqlite3

import pytest

from hermes_cli import worker_account_boards as inventory
from hermes_cli import worker_board_retirement as retirement
from hermes_cli import worker_compute_capacity as capacity


@pytest.fixture
def board(tmp_path, monkeypatch):
    account = tmp_path / 'account'
    account.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: account)
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr('hermes_constants.get_default_hermes_root', lambda: account)
    path = tmp_path / 'foreign' / 'kanban.db'
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE tasks (status TEXT)')
        inventory.register(conn)
    return path


def test_remover_publishes_idle_proof_before_filesystem_change(board):
    record, intent = retirement.record_paths(board)
    with inventory.removing(board):
        proof = json.loads(intent.read_text())
        assert proof == {'version': 1, 'path': str(board), 'idle': True,
                         'device': board.stat().st_dev, 'inode': board.stat().st_ino}
        assert record.exists()
        board.unlink()
    assert not record.exists()
    assert not intent.exists()


def test_crash_after_rename_recovers_only_matching_proof(board, monkeypatch):
    record, intent = retirement.record_paths(board)
    discard = retirement.discard
    def interrupted(path):
        if path == record:
            raise OSError('simulated process interruption before retirement')
        discard(path)
    monkeypatch.setattr(retirement, 'discard', interrupted)
    with pytest.raises(OSError, match='interruption'):
        with inventory.removing(board):
            board.rename(board.with_suffix('.archived'))
    assert record.exists() and intent.exists()
    with pytest.raises(FileNotFoundError):
        inventory.paths()
    monkeypatch.setattr(retirement, 'discard', discard)
    retirement.recover(board)
    assert not record.exists() and not intent.exists()
    assert inventory.paths() == []


def test_registration_invalidates_old_idle_proof_before_claim(board):
    retirement.prepare(board)
    with sqlite3.connect(board) as conn:
        inventory.register(conn)
    _, intent = retirement.record_paths(board)
    assert not intent.exists()
    board.unlink()
    with pytest.raises(FileNotFoundError):
        retirement.recover(board)


def test_recovery_refuses_unproven_missing_database(board):
    record, _ = retirement.record_paths(board)
    board.unlink()
    with pytest.raises(FileNotFoundError):
        retirement.recover(board)
    assert record.exists()


def test_recovery_refuses_existing_source_or_different_identity(board):
    record, intent = retirement.record_paths(board)
    retirement.prepare(board)
    with pytest.raises(ValueError, match='source exists'):
        retirement.recover(board)
    board.unlink()
    raw = json.loads(intent.read_text())
    raw['path'] += '.other'
    intent.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='invalid board retirement proof'):
        retirement.recover(board)
    assert record.exists()


def test_recovery_is_fenced_against_claims(board):
    from hermes_cli.admission_fence import admission_lock, AdmissionBusy
    retirement.prepare(board)
    board.unlink()
    record, intent = retirement.record_paths(board)
    with admission_lock(capacity.policy_root(), timeout=0):
        with pytest.raises(AdmissionBusy):
            retirement.recover(board)
    assert record.exists() and intent.exists()


def test_claim_with_disabled_policy_invalidates_proof_under_account_fence(board, monkeypatch):
    from hermes_cli import kanban_admission as admission
    from hermes_cli.admission_fence import admission_lock
    retirement.prepare(board)
    _, intent = retirement.record_paths(board)
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: False)
    monkeypatch.setattr('hermes_constants.get_default_hermes_root', lambda: board.parent)
    monkeypatch.setattr(admission, '_final_hub_gate', lambda *a: (None, None))
    with sqlite3.connect(board) as conn:
        with admission_lock(capacity.policy_root(), timeout=0):
            with admission.claim_transaction(conn) as accepted:
                assert not accepted
            assert intent.exists()
        with admission.claim_transaction(conn) as accepted:
            assert accepted
            assert not intent.exists()
    board.unlink()
    with pytest.raises(FileNotFoundError):
        retirement.recover(board)


def test_failed_removal_cancels_proof(board):
    record, intent = retirement.record_paths(board)
    with pytest.raises(RuntimeError):
        with inventory.removing(board):
            raise RuntimeError('before rename')
    assert record.exists() and not intent.exists()
