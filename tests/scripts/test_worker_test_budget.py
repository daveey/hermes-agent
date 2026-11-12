"""Worker test admission must not be widened by per-run environment."""
import os

import pytest

from hermes_cli import worker_compute_capacity as capacity
from scripts import run_tests_host_slots as slots


@pytest.mark.parametrize('requested', ['0', '64', '1'])
def test_worker_fixed_host_domain(monkeypatch, requested, tmp_path):
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(capacity, 'effective_cores', lambda: 16)
    monkeypatch.setenv('HERMES_TEST_HOST_SLOTS', requested)
    monkeypatch.setenv('HERMES_TEST_SLOT_DIR', '/noncanonical')
    monkeypatch.setenv('HERMES_TEST_XDIST_MAX', '64')
    monkeypatch.setenv(slots.HELD_ENV, '1')
    assert slots.host_slot_count() == 4
    assert slots.host_slot_dir() == f'/tmp/hermes-test-slots-{os.getuid()}'
    assert slots.xdist_worker_budget() == 1
    assert slots.host_slots_enabled(), 'a bare inherited flag proves no lock'
    monkeypatch.setattr(slots, 'host_slot_dir', lambda: str(tmp_path))
    def unexpected_lock(path):
        pytest.fail('invalid inherited receipt must not wait for another slot')
    monkeypatch.setattr(slots, '_try_slot', unexpected_lock)
    with pytest.raises(RuntimeError, match='invalid inherited'):
        slots.acquire_host_slot()


def test_receipt_requires_live_exclusive_lock(monkeypatch, tmp_path):
    from scripts import worker_test_budget as budget
    import fcntl
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(slots, 'host_slot_dir', lambda: str(tmp_path))
    monkeypatch.setattr(slots, 'host_slot_count', lambda: 1)
    monkeypatch.delenv(budget.RECEIPT_ENV, raising=False)
    monkeypatch.delenv(slots.HELD_ENV, raising=False)
    with slots.host_test_slot() as fd:
        assert fd is not None
        env = {}
        budget.child_environment(env, fd)
        monkeypatch.setenv(budget.RECEIPT_ENV, env[budget.RECEIPT_ENV])
        assert not slots.host_slots_enabled()
        # Same PID/fd/inode can be reacquired for another file attempt. A stray
        # grandchild's old receipt must not borrow that later acquisition.
        slots._write_holder(fd)
        assert slots.host_slots_enabled(), 'receipt must bind acquisition nonce'
        budget.child_environment(env, fd)
        monkeypatch.setenv(budget.RECEIPT_ENV, env[budget.RECEIPT_ENV])
        assert not slots.host_slots_enabled()
        fcntl.flock(fd, fcntl.LOCK_UN)
        assert slots.host_slots_enabled(), 'unlocked descriptor is not a receipt'
        fcntl.flock(fd, fcntl.LOCK_SH)
        assert slots.host_slots_enabled(), 'shared lock is not a capacity slot'
    assert slots.host_slots_enabled(), 'closed receipt cannot waive admission'


def test_inherited_controller_still_caps_xdist(monkeypatch):
    from tests import conftest
    from types import SimpleNamespace
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(slots, 'host_slots_enabled', lambda: False)
    monkeypatch.setattr(conftest, '_host_slots_module', lambda: slots)
    config = SimpleNamespace(option=SimpleNamespace(tx=['popen'] * 16, numprocesses=16))
    conftest._take_host_test_slot(config)
    assert config.option.tx == ['popen']
    assert os.environ['OPENBLAS_NUM_THREADS'] == '1'


def test_worker_slot_errors_fail_closed(monkeypatch):
    from tests import conftest
    from types import SimpleNamespace
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(conftest, '_host_slots_module', lambda: slots)
    def broken():
        raise OSError('controller unavailable')
    monkeypatch.setattr(slots, 'host_slots_enabled', broken)
    with pytest.raises(pytest.UsageError, match='controller unavailable'):
        conftest._take_host_test_slot(SimpleNamespace())


def test_worker_never_reclaims_held_inode(monkeypatch, tmp_path):
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(slots, 'host_slot_dir', lambda: str(tmp_path))
    monkeypatch.setattr(slots, 'host_slot_count', lambda: 1)
    monkeypatch.setattr(slots, '_stale_reason', lambda *a: ('old', False))
    monkeypatch.setattr(slots, 'slot_max_age_s', lambda: 1)
    monkeypatch.delenv(slots.HELD_ENV, raising=False)
    with slots.host_test_slot():
        inode = (tmp_path / '0.lock').stat().st_ino
        assert slots.reclaim_stale_slots(str(tmp_path), 1) == []
        assert (tmp_path / '0.lock').stat().st_ino == inode


def test_receipt_in_real_child_and_bad_identities(monkeypatch, tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from scripts import worker_test_budget as budget
    from tools.environments.local import build_subprocess_env
    monkeypatch.setattr(capacity, 'worker_enabled', lambda: True)
    monkeypatch.setattr(slots, 'host_slot_dir', lambda: str(tmp_path))
    monkeypatch.setattr(slots, 'host_slot_count', lambda: 1)
    monkeypatch.delenv(slots.HELD_ENV, raising=False)
    monkeypatch.delenv(budget.RECEIPT_ENV, raising=False)
    code = '''
import json, os, sys
from scripts import run_tests_host_slots as slots, worker_test_budget as budget
slots.host_slot_dir = lambda: sys.argv[1]
slots.host_slot_count = lambda: 1
assert budget.inherited_slot(), 'real ancestor lock must validate without inherited fd'
original = os.environ[budget.RECEIPT_ENV]
for field, value in [('pid', 1), ('birth', -1), ('fd', 999999), ('slot', 1), ('nonce', 'wrong')]:
    rec = json.loads(original)
    rec[field] = value
    os.environ[budget.RECEIPT_ENV] = json.dumps(rec)
    assert not budget.inherited_slot(), field
print('child receipt verified')
'''
    with slots.host_test_slot() as fd:
        env = build_subprocess_env()
        budget.child_environment(env, fd)
        result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], env=env,
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, encoding='utf-8', timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'child receipt verified' in result.stdout
