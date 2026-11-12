"""A head cancelled while its preclaim check conflicts must not strand its queued successor.

The notifier-drain selection resolves the attempted queued identity instead of the FIFO
front. When that identity is no longer queued (a /stop cancel committed from a worker
thread during the async check), the drain re-reads and runs the follower rather than
pausing ``admission_conflict`` with the follower still queued.
"""
import asyncio

import pytest

from hermes_state_runtime import RuntimeStoreError, cancel_session_input, list_session_admissions
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit


@pytest.mark.asyncio
async def test_successor_runs_when_conflicted_head_is_cancelled_mid_check(tmp_path, monkeypatch):
    from gateway import session_api_turn, session_finite
    from gateway.config import Platform
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed, ids = [], {}

    def check_api_turn(authority_, ref, payload):
        if payload['text'] == 'stale':
            # A /stop cancel commits from a worker thread while the head's check runs.
            cancel_session_input(db, epoch=authority.epoch, admission_id=ids['stale'])
            authority._settle_cancelled(REF, ids['stale'])
            raise RuntimeStoreError('admission_conflict')
    monkeypatch.setattr(session_api_turn, 'check_api_turn', check_api_turn)

    async def execute(authority, ref, row):
        executed.append(row['payload']['text'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        authority._schedule = lambda ref: None
        ids['stale'] = (await _submit(authority, 'stale')).admission_id
        await _submit(authority, 'follower')
        del authority._schedule
        authority._schedule(REF)
        await asyncio.wait_for(authority.sessions['s'].task, 5)
        statuses = {r['request_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert executed == ['follower'], statuses
        assert statuses['stale'][0] != 'queued' and statuses['follower'][0] != 'queued', statuses
