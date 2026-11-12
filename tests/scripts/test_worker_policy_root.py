"""Real conftest sandbox entry must not switch off host CPU policy."""
import subprocess
import sys
from pathlib import Path

import pytest

from tools.environments.local import build_subprocess_env


@pytest.mark.parametrize('enabled', [True, False])
def test_conftest_sandbox_preserves_account_worker_policy(tmp_path, enabled):
    account = tmp_path / 'account'
    root = account / '.hermes'
    root.mkdir(parents=True)
    (root / 'config.yaml').write_text(
        f'kanban:\n  worker_resources:\n    enabled: {str(enabled).lower()}\n')
    code = r'''
import os, pwd, sys
from pathlib import Path
from types import SimpleNamespace
# Replace only OS-account discovery, not worker policy or the test-slot gate.
real = pwd.getpwuid
account, enabled = sys.argv[1], sys.argv[2] == 'True'
pwd.getpwuid = lambda uid: SimpleNamespace(pw_dir=account)
import hermes_constants
from hermes_cli import worker_compute_capacity as capacity
assert capacity.policy_root() == Path(account) / '.hermes'
assert capacity.worker_enabled() is enabled
capacity.effective_cores = lambda: 16
from tests import conftest
assert Path(os.environ['HERMES_HOME']) != capacity.policy_root()
assert capacity.worker_enabled() is enabled
from scripts import run_tests_host_slots as slots
assert slots.xdist_worker_budget() == (1 if enabled else 64)
# Forged inherited metadata must be rejected after conftest's sandbox redirect.
os.environ[slots.HELD_ENV] = '1'
os.environ['HERMES_TEST_SLOT_RECEIPT'] = '{}'
import pytest
config = SimpleNamespace(option=SimpleNamespace(tx=['popen'] * 16, numprocesses=16))
try:
    conftest._take_host_test_slot(config)
except pytest.UsageError as error:
    assert enabled and 'invalid inherited' in str(error), str(error)
else:
    assert not enabled
print('policy survived conftest sandbox')
'''
    env = build_subprocess_env()
    env.update(HOME=str(tmp_path / 'foreign-home'), HERMES_HOME=str(tmp_path / 'foreign-profile'),
               HERMES_TEST_XDIST_MAX='64')
    result = subprocess.run([sys.executable, '-c', code, str(account), str(enabled)],
                            cwd=Path(__file__).resolve().parents[2], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'policy survived conftest sandbox' in result.stdout
