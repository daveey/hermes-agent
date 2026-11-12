"""Account capacity serializes installation roots through real SQL commit."""
import sqlite3

import pytest

from hermes_cli import admission_fence as fence
from hermes_cli import kanban_admission as admission
from hermes_cli import worker_compute_capacity as capacity


@pytest.fixture
def account(monkeypatch, tmp_path):
    root = tmp_path / 'account'
    root.mkdir()
    monkeypatch.setattr(capacity, 'policy_root', lambda: root)
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(admission, '_final_hub_gate', lambda *a: (None, None))
    return root


def connection(path):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE sample (value INTEGER)')
    conn.commit()
    return conn


def test_two_installations_share_account_lock_through_commit(account, monkeypatch, tmp_path):
    import hermes_constants
    first = connection(tmp_path / 'one.db')
    second = connection(tmp_path / 'two.db')
    try:
        monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: tmp_path / 'install-one')
        with admission.claim_transaction(first) as accepted:
            assert accepted
            first.execute('INSERT INTO sample VALUES (1)')
            monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: tmp_path / 'install-two')
            with admission.claim_transaction(second) as other:
                assert not other
            with pytest.raises(fence.AdmissionBusy):
                with fence.admission_lock(account, timeout=0):
                    pass
        with fence.admission_lock(account, timeout=0):
            assert first.execute('SELECT * FROM sample').fetchall() == [(1,)]
        with admission.claim_transaction(second) as accepted:
            assert accepted
    finally:
        first.close()
        second.close()


def test_account_lock_released_after_rollback(account, monkeypatch, tmp_path):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: tmp_path / 'installation')
    conn = connection(tmp_path / 'rollback.db')
    try:
        with pytest.raises(RuntimeError, match='rollback'):
            with admission.claim_transaction(conn) as accepted:
                assert accepted
                conn.execute('INSERT INTO sample VALUES (1)')
                raise RuntimeError('rollback')
        assert conn.execute('SELECT * FROM sample').fetchall() == []
        with fence.admission_lock(account, timeout=0):
            pass
    finally:
        conn.close()


def test_same_account_and_installation_does_not_self_contend(account, monkeypatch, tmp_path):
    import hermes_constants
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: account)
    conn = connection(tmp_path / 'same.db')
    try:
        with admission.claim_transaction(conn) as accepted:
            assert accepted
    finally:
        conn.close()


def test_installation_update_hold_still_refuses(account, monkeypatch, tmp_path):
    import hermes_constants
    root = tmp_path / 'installation'
    marker = root / 'core-update' / 'holds' / 'hermes-sync'
    marker.parent.mkdir(parents=True)
    marker.touch()
    monkeypatch.setattr(hermes_constants, 'get_default_hermes_root', lambda: root)
    conn = connection(tmp_path / 'held.db')
    try:
        with admission.claim_transaction(conn) as accepted:
            assert not accepted
    finally:
        conn.close()
