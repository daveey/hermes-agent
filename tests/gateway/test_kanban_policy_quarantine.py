"""A pre-admission Kanban retry reuses and respects its quarantined receipt."""
import json

import pytest

from hermes_state_local import POLICY_PREFIX
from hermes_state_runtime import RuntimeStoreError
from tests.gateway.test_kanban_finite_admission import kanban_owner


@pytest.mark.asyncio
async def test_kanban_invalid_policy_retries_keep_one_receipt(kanban_owner, monkeypatch):
    from gateway import session_kanban
    from gateway.session_local_recovery import local_receipt_ids
    from dataclasses import replace

    owner = kanban_owner({'model': {'default': 'm', 'provider': 'custom'}})
    build = session_kanban.build_kanban_policy

    def broken(*args):
        policy, secrets = build(*args)
        return replace(policy, model=''), secrets

    monkeypatch.setattr(session_kanban, 'build_kanban_policy', broken)
    try:
        for _ in range(3):
            with pytest.raises(RuntimeStoreError, match='invalid_params'):
                await session_kanban.run_task(owner.connection, owner.params)
        with owner.authority.db._read_ctx() as conn:
            rows = conn.execute('SELECT value FROM state_meta WHERE key LIKE ?', (POLICY_PREFIX + '%',)).fetchall()
            assert len(rows) == 1
            receipt = json.loads(rows[0][0])
            assert receipt['policy']['model'] == ''
            assert receipt['quarantine']['reason'] == 'invalid_stored_policy'
            assert conn.execute('SELECT count(*) FROM session_admissions').fetchone()[0] == 0
        assert receipt['session_id'] not in local_receipt_ids(owner.authority)
    finally:
        await owner.close()
