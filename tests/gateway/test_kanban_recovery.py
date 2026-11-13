"""Real dispatch preserves custom board identity and unknown attempts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


# The probe traces the transport client's opens with strace (Linux-only tool, Linux openat
# flags); without it the probe cannot prove the client never opens state.db writable.
@pytest.mark.platforms('linux')
@pytest.mark.skipif(shutil.which('strace') is None, reason='requires strace on PATH')
@pytest.mark.parametrize('mode,contention', [('custom', False), ('cross_profile', False),
                                           ('restart', False), ('restart', True)],
                         ids=['custom', 'cross_profile', 'restart', 'restart-contention'])
def test_owner_board_recovery(tmp_path, mode, contention):
    repo = Path(__file__).resolve().parents[2]
    env = {k: os.environ[k] for k in ('PATH', 'LANG', 'TZ', 'TIRITH_ENABLED') if k in os.environ}
    env.update(HOME=str(tmp_path / 'user'), HERMES_HOME=str(tmp_path / 'state'),
               PYTHONPATH=str(repo), KANBAN_RECOVERY_MODE=mode)
    command = [sys.executable, str(repo / 'tests/gateway/fixtures/kanban_recovery_probe.py')]
    if contention:
        command.append('--admission-contention')
    result = subprocess.run(command,
                            cwd=repo, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((tmp_path / 'state/receipt.json').read_text())
    print(json.dumps(receipt))
    # Neither the probe's claims nor its children read the account's real host policy or worker inventory.
    policy = Path(receipt['policy_root'])
    assert policy.is_relative_to(tmp_path) and any(policy.glob('child-*')), policy
