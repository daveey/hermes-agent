"""Stored policy validation is terminal; unavailable storage is retryable."""
import json

import pytest

from gateway import session_local_recovery as recovery
from gateway.session_local import create_local_session
from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_local_receipt_untracked_compression import _owner, _unrecovered_owner


def _set_policy(key, value):
    return lambda receipt: receipt['policy'].__setitem__(key, value)


DAMAGE = {
    'empty_model': _set_policy('model', ''),
    'missing_model': lambda receipt: receipt['policy'].pop('model'),
    'bad_toolsets': _set_policy('toolsets', 42),
    'bad_json': _set_policy('config_json', '{'),
    'missing_policy': lambda receipt: receipt.pop('policy'),
}


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', list(DAMAGE))
async def test_invalid_policy_is_quarantined_once(tmp_path, monkeypatch, caplog, damage):
    first, connection = await _owner(tmp_path, monkeypatch, 'first')
    ref = create_local_session(first, connection.actor, {
        'request_id': 'r', 'source': 'tui', 'cwd': str(tmp_path), 'model': 'm', 'toolsets': []})
    sid = ref.session_id
    first.db.append_message(sid, 'user', 'preserve this history; never replay')
    with first.db._read_ctx() as conn:
        receipt = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?',
                                         (POLICY_PREFIX + sid,)).fetchone()[0])
    DAMAGE[damage](receipt)
    first.db._execute_write(lambda conn: conn.execute(
        'UPDATE state_meta SET value=? WHERE key=?', (json.dumps(receipt), POLICY_PREFIX + sid)))
    await connection.close()
    first.db.close()
    second, connection = _unrecovered_owner(tmp_path, 'second')
    try:
        recovery.recover_local_sessions(second)
        assert sid not in recovery.local_receipt_ids(second)
        assert sid not in second.sessions
        with second.db._read_ctx() as conn:
            saved = json.loads(conn.execute('SELECT value FROM state_meta WHERE key=?',
                                           (POLICY_PREFIX + sid,)).fetchone()[0])
        assert saved.get('policy') == receipt.get('policy')
        assert saved['quarantine']['reason'] == 'invalid_stored_policy'
        assert second.db.get_messages_as_conversation(sid)[0]['content'] == 'preserve this history; never replay'
        recovery.recover_local_sessions(second)
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            recovery.restore_local_session(second, sid)
        warnings = [r for r in caplog.records if sid in r.getMessage() and r.levelname == 'WARNING']
        assert len(warnings) == 1
    finally:
        await connection.close()
        second.db.close()
    third, connection = _unrecovered_owner(tmp_path, 'third')
    try:
        assert sid not in recovery.local_receipt_ids(third)
        recovery.recover_local_sessions(third)
        assert sid not in third.sessions
    finally:
        await connection.close()
        third.db.close()


@pytest.mark.asyncio
async def test_transient_policy_restore_failure_retries(tmp_path, monkeypatch):
    from gateway import session_policy
    first, connection = await _owner(tmp_path, monkeypatch, 'first')
    ref = create_local_session(first, connection.actor, {
        'request_id': 'r', 'source': 'tui', 'cwd': str(tmp_path), 'model': 'm', 'toolsets': []})
    await connection.close()
    first.db.close()
    second, connection = _unrecovered_owner(tmp_path, 'second')
    original = session_policy.restore_policy
    calls = []

    def unavailable(data):
        calls.append(data)
        raise RuntimeStoreError('storage_unavailable')

    try:
        monkeypatch.setattr(session_policy, 'restore_policy', unavailable)
        recovery.recover_local_sessions(second)
        recovery.recover_local_sessions(second)
        assert len(calls) == 2
        assert ref.session_id in recovery.local_receipt_ids(second)
        monkeypatch.setattr(session_policy, 'restore_policy', original)
        recovery.recover_local_sessions(second)
        assert ref.session_id in second.sessions
    finally:
        await connection.close()
        second.db.close()
