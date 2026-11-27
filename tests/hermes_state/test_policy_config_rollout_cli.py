"""Exercise the real module entrypoint on a disposable store, not mocked calls."""
from contextlib import closing
import json
import subprocess
import sys

from hermes_state import SessionDB
from hermes_state_local import local_receipt
import hermes_state_runtime as rt
from tests.hermes_state.test_policy_config_rollout import create


def test_actual_cli_requires_ack_and_defaults_to_read_only(tmp_path):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid, expected = create(db, epoch, tmp_path, False)
    command = [sys.executable, '-m', 'hermes_state_policy_rollout', '--db', str(path), '--direction', 'intern']
    preview = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)['applied'] is False
    refused = subprocess.run([*command, '--apply'], capture_output=True, text=True, timeout=30, check=False)
    assert refused.returncode == 2
    assert json.loads(refused.stdout)['error'] == 'runtime_coordination_required'
    applied = subprocess.run([*command, '--apply', '--readers-compatible'], capture_output=True, text=True, timeout=30, check=False)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)['converted'] == 1
    with closing(SessionDB(path, read_only=True)) as db:
        assert local_receipt(db, sid) == expected
