"""Disk brake exercises real ready/review claims, not monitor hold mocks."""
from types import SimpleNamespace

import pytest

from hermes_cli import admission_fence as af
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_disk_admission as disk
from tests.hermes_cli import test_kanban_admission_contention as helpers

board = helpers.board


@pytest.fixture
def pressure(board, monkeypatch):
    conn, home = board
    config = home / 'config.yaml'
    config.write_text('kanban:\n  admission:\n    disk_free_floor_bytes: 10\n')
    space = [5]
    monkeypatch.setattr(disk.os, 'statvfs', lambda path: SimpleNamespace(f_bavail=space[0], f_frsize=1))
    return conn, home, space


@pytest.mark.parametrize('review', [False, True])
def test_low_space_refuses_before_board_lease_and_recovers(pressure, monkeypatch, review):
    conn, home, space = pressure
    task = helpers._task(conn, review)
    claim = kb.claim_review_task if review else kb.claim_task
    real_gate = kb._sync.claim_gate
    calls = []
    def gate(*args, **kwargs):
        calls.append(True)
        return real_gate(*args, **kwargs)
    monkeypatch.setattr(kb._sync, 'claim_gate', gate)
    assert claim(conn, task) is None
    assert calls == []
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0
    # Normal turns are independent; no installation-wide hold was written.
    reservation = af.reserve(home)
    assert reservation is not None
    reservation.close()
    space[0] = 10
    assert claim(conn, task).status == 'running'
    assert calls == [True]
    space[0] = 0
    assert kb.get_task(conn, task).status == 'running'


@pytest.mark.parametrize('review', [False, True])
def test_space_falls_after_lease_final_gate_refuses(pressure, monkeypatch, review):
    conn, _home, space = pressure
    task = helpers._task(conn, review)
    space[0] = 20
    real_gate = kb._sync.claim_gate
    def gate(*args, **kwargs):
        result = real_gate(*args, **kwargs)
        space[0] = 1
        return result
    monkeypatch.setattr(kb._sync, 'claim_gate', gate)
    assert (kb.claim_review_task if review else kb.claim_task)(conn, task) is None
    assert conn.execute('SELECT count(*) FROM task_runs').fetchone()[0] == 0


def test_recovery_does_not_clear_operator_hold(pressure):
    conn, home, space = pressure
    task = helpers._task(conn, False)
    af.set_hold(home)
    space[0] = 100
    assert kb.claim_task(conn, task) is None
    assert af.is_held(home / 'core-update')


def test_sample_failure_closed(pressure, monkeypatch):
    conn, _home, _space = pressure
    task = helpers._task(conn, False)
    def fail(path):
        raise OSError('unavailable')
    monkeypatch.setattr(disk.os, 'statvfs', fail)
    assert kb.claim_task(conn, task) is None


@pytest.mark.parametrize('value', ['true', '-1', '0', 'garbage', '[]'])
def test_invalid_enabled_floor_refuses(pressure, value):
    conn, home, space = pressure
    (home / 'config.yaml').write_text(f'kanban:\n  admission:\n    disk_free_floor_bytes: {value}\n')
    space[0] = 100
    assert kb.claim_task(conn, helpers._task(conn, False)) is None


@pytest.mark.parametrize('config', ['{}\n', '', '# empty\n', 'kanban: null\n', 'kanban:\n  admission: null\n'])
def test_unconfigured_host_unchanged(pressure, monkeypatch, config):
    conn, home, _space = pressure
    (home / 'config.yaml').write_text(config)
    def unexpected(path):
        pytest.fail('unconfigured host must not sample disk')
    monkeypatch.setattr(disk.os, 'statvfs', unexpected)
    assert kb.claim_task(conn, helpers._task(conn, False)).status == 'running'


@pytest.mark.parametrize('config', ['kanban: []\n', 'kanban:\n  admission: []\n', 'kanban: [broken\n'])
def test_malformed_policy(pressure, config):
    _conn, home, _space = pressure
    (home / 'config.yaml').write_text(config)
    assert disk.refusal_reason() == 'worker_disk_policy_unknown'


def test_installation_policy_wins_over_named_profile(pressure, monkeypatch):
    _conn, home, space = pressure
    profile = home / 'profiles' / 'reviewer'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_HOME', str(profile))
    assert disk.refusal_reason() == 'worker_disk_below_floor'
    space[0] = 10
    assert disk.refusal_reason() is None


def test_unsupported_fence_still_checks_final_gate(pressure, monkeypatch):
    from hermes_cli.kanban_admission import claim_transaction
    conn, _home, space = pressure
    monkeypatch.setattr(af, 'SUPPORTED', False)
    with claim_transaction(conn) as admitted:
        assert not admitted
    space[0] = 10
    with claim_transaction(conn) as admitted:
        assert admitted


def test_missing_config_is_disabled(pressure):
    _conn, home, _space = pressure
    (home / 'config.yaml').unlink()
    assert disk.refusal_reason() is None


def test_negative_sample_is_unknown(pressure):
    _conn, _home, space = pressure
    space[0] = -1
    assert disk.refusal_reason() == 'worker_disk_unknown'
