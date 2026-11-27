"""Exercise the real module entrypoint on a disposable store, not mocked calls."""
from contextlib import closing
import json
import sqlite3
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize('error', [rt.RuntimeStoreError('private payload', detail='secret'),
                                   sqlite3.OperationalError('secret')], ids=['runtime', 'sqlite'])
def test_cli_storage_failures_do_not_echo_exception_payloads(tmp_path, monkeypatch, capsys, error):
    import hermes_state_policy_rollout as rollout
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)):
        pass

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(rollout, 'convert_policy_config_batch', fail)
    assert rollout.main(['--db', str(path), '--direction', 'inline']) == 2
    assert json.loads(capsys.readouterr().out) == {'error': 'storage_unavailable'}


@pytest.mark.parametrize('option,value', [('--limit', '0'), ('--limit', '64'),
                                        ('--max-bytes', '0'), ('--max-bytes', '4194305')])
@pytest.mark.parametrize('apply', [False, True])
def test_actual_cli_invalid_bounds_are_not_storage_failures(tmp_path, option, value, apply):
    path = tmp_path / 'state.db'
    with closing(SessionDB(path)) as db:
        epoch = rt.begin_runtime_epoch(db, instance_id='owner')
        sid, expected = create(db, epoch, tmp_path, False)
        with db._read_ctx() as conn:
            before = conn.execute('SELECT key,value FROM state_meta ORDER BY key').fetchall()
    command = [sys.executable, '-m', 'hermes_state_policy_rollout', '--db', str(path),
               '--direction', 'intern', option, value]
    if apply:
        command += ['--apply', '--readers-compatible']
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout) == {'error': 'invalid_params'}
    with closing(SessionDB(path, read_only=True)) as db:
        with db._read_ctx() as conn:
            assert conn.execute('SELECT key,value FROM state_meta ORDER BY key').fetchall() == before
        assert local_receipt(db, sid) == expected
