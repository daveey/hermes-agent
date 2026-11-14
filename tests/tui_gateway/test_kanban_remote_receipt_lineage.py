"""Remote receipt custody follows the admitted physical target, not arbitrary ancestry."""
import json

import pytest

from gateway.session_results import finish_result
from hermes_state_runtime import admit_session_input, claim_session_input
from tests.gateway.test_resume_latest_local_cli import _owner, _call, _reset
from tui_gateway.kanban_remote_receipts import completed_notice

KEY = 'remote:worker:default/default/t_0000000000000001:123'


@pytest.mark.asyncio
async def test_physical_reset_target_keeps_exact_admission_custody(tmp_path, monkeypatch):
    store, authority, connection = _owner(tmp_path / 'home', monkeypatch)
    try:
        created = await _call(connection, 'session.create', request_id='chat', source='tui',
                              cwd=str(tmp_path), toolsets=[], model='m')
        sid = created['session_id']
        physical = _reset(authority, store, sid, 'reset-child')
        db = authority.db
        route = authority.sessions[sid].route
        identity = json.dumps(['kanban-wake', route, 'event'])
        payload = {'text': 'card', 'local_automation_v1': {'identity': identity, 'owner': sid,
            'route': route, 'target': physical, 'display_kind': 'kanban_notice', 'kanban_event_keys': [KEY]}}
        admit_session_input(db, epoch=authority.epoch, principal_id='automation:' + route,
                            session_id=sid, request_id=identity, payload=payload)
        row = claim_session_input(db, epoch=authority.epoch, session_id=sid)
        finish_result(db, epoch=authority.epoch, row=row, response='done', outcome='completed', result={
            'result': {'completed': True, 'agent_persisted': True, 'messages': []}})
        assert completed_notice(db, physical, KEY)
        # A subsequent reset is not in this admission's physical compression lineage.
        from hermes_state_local import local_receipt
        from hermes_state_local_lineage import reset_local_target
        entry = dict(local_receipt(db, sid)['entry'], session_id='next-reset')
        reset_local_target(db, epoch=authority.epoch, parent_session_id=physical, entry=entry)
        assert not completed_notice(db, 'next-reset', KEY)
        db.create_session('branch', 'desktop', parent_session_id=physical)
        assert not completed_notice(db, 'branch', KEY)
    finally:
        await connection.close()
        store.close_all_db_handles()
