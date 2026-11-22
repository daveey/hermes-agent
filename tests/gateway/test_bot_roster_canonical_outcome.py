"""Both roster producers carry the stored canonical envelope, never a recency guess."""
import json

import pytest

from gateway.session_group_controls import _read_bot_sessions
from hermes_state import SessionDB
from tui_gateway.server import _canonical_session_row


@pytest.mark.parametrize('producer', ['gateway', 'legacy'])
@pytest.mark.parametrize('raw', [None, '{broken', json.dumps({'kind': 'done', 'status': {
    'status_version': 1, 'revision': 8, 'generation': 3,
    'identity': {'gateway': 'example', 'profile': 'alpha', 'session_id': 'bot-chat', 'owner_epoch': 'epoch'},
    'kind': 'done', 'coverage': {'state': 'complete', 'reasons': []},
    'presentation': {'stripe': 'done', 'reason_codes': []}}})])
def test_canonical_roster_carries_stored_outcome(tmp_path, producer, raw):
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('bot-chat', source='gui', user_id='uid:desk', chat_id='local-bot-chat')
        db._write_rowcount('UPDATE sessions SET title=?, outcome_json=? WHERE id=?',
                           ('Bot Chat', raw, 'bot-chat'))
        if producer == 'gateway':
            row = _read_bot_sessions(db, 'uid:desk', False)['canonical_session']
        else:
            row = _canonical_session_row(db, tmp_path)
        assert row is not None
        assert row['outcome'] == (json.loads(raw) if raw and raw != '{broken' else None)
    finally:
        db.close()
