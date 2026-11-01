"""Quarantine cannot commit against stale ownership or a changed receipt."""
import json

import pytest

from gateway.session_local import create_local_session
from gateway.session_local_recovery import local_receipt_ids, recover_local_sessions
from hermes_state_local import POLICY_PREFIX, local_receipt
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_local_receipt_untracked_compression import _owner, _unrecovered_owner


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unavailable', 'changed_receipt'])
async def test_quarantine_write_failure_keeps_receipt_retryable(tmp_path, monkeypatch, failure):
    first, connection = await _owner(tmp_path, monkeypatch, 'first')
    ref = create_local_session(first, connection.actor, {
        'request_id': 'r', 'source': 'tui', 'cwd': str(tmp_path), 'model': 'm', 'toolsets': []})
    sid = ref.session_id
    receipt = local_receipt(first.db, sid)
    receipt['policy']['model'] = ''
    first.db._execute_write(lambda conn: conn.execute(
        'UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + sid)))
    await connection.close()
    first.db.close()
    second, connection = _unrecovered_owner(tmp_path, 'second')
    write = second.db._execute_write

    def interrupted_write(operation):
        if failure == 'unavailable':
            raise RuntimeStoreError('storage_unavailable')
        changed = dict(receipt, concurrent_metadata=True)
        write(lambda conn: conn.execute('UPDATE state_meta SET value=? WHERE key=?',
                                       (json.dumps(changed), POLICY_PREFIX + sid)))
        return write(operation)

    try:
        monkeypatch.setattr(second.db, '_execute_write', interrupted_write)
        recover_local_sessions(second)
        assert sid in local_receipt_ids(second)
        assert 'quarantine' not in local_receipt(second.db, sid)
        monkeypatch.setattr(second.db, '_execute_write', write)
        recover_local_sessions(second)
        assert sid not in local_receipt_ids(second)
        assert local_receipt(second.db, sid)['quarantine']['reason'] == 'invalid_stored_policy'
    finally:
        await connection.close()
        second.db.close()
