"""Proof-chat visibility and retirement survive real compression publication."""
import json
import time

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize('state', ['ended', 'done', 'idle', 'active'])
@pytest.mark.parametrize('hidden', [True, False])
def test_compressed_agent_cli_keeps_visibility_and_retires(tmp_path, state, hidden):
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('parent', 'cli', model_config={'_agent_cli': True})
        db.set_session_hidden('parent', hidden)
        db.publish_compression_child(
            parent_session_id='parent', child_session_id='child', source='cli',
            messages=[{'role': 'user', 'content': 'proof'}],
            require_compression_lease=False)
        child = db.get_session('child')
        assert bool(child['hidden']) is hidden
        assert json.loads(child['model_config'])['_agent_cli'] is True
        if state == 'ended':
            db.end_session('child', 'done')
        elif state == 'done':
            db._write_sql('UPDATE sessions SET outcome_json=? WHERE id=?', ('{"kind":"done"}', 'child'))
        elif state == 'idle':
            then = time.time() - 90000
            db._write_sql('UPDATE sessions SET started_at=?, last_activity_at=?', (then, then))
            db._write_sql('UPDATE messages SET timestamp=?', (then,))
        db.maybe_archive_empty_drafts()
        for sid in ('parent', 'child'):
            assert bool(db.get_session(sid)['archived']) is (hidden and state != 'active')
    finally:
        db.close()
