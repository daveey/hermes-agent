"""Hidden proof CLI chats retire independently of human auto-archive preferences."""
import time

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize('state', ['ended', 'done', 'idle', 'fresh'])
def test_agent_cli_retirement_does_not_archive_human_chats(tmp_path, state):
    db = SessionDB(tmp_path / 'state.db')
    try:
        for sid, agent, hidden in [('agent', True, True), ('human', False, False),
                                   ('manual-hidden', False, True), ('visible-agent', True, False)]:
            db.create_session(sid, 'cli', model_config={'_agent_cli': agent})
            db.set_session_hidden(sid, hidden)
            db.append_message(sid, 'user', 'proof')
            if state == 'ended':
                db.end_session(sid, 'done')
            elif state == 'done':
                db._write_sql('UPDATE sessions SET outcome_json=? WHERE id=?', ('{"kind":"done"}', sid))
            elif state == 'idle':
                then = time.time() - 90000
                db._write_sql('UPDATE sessions SET started_at=?, last_activity_at=? WHERE id=?', (then, then, sid))
                db._write_sql('UPDATE messages SET timestamp=? WHERE session_id=?', (then, sid))
        db.maybe_archive_empty_drafts()  # ungated maintenance entrypoint
        assert bool(db.get_session('agent')['archived']) is (state != 'fresh')
        for sid in ('human', 'manual-hidden', 'visible-agent'):
            assert not db.get_session(sid)['archived']
    finally:
        db.close()


def test_classic_create_visibility(tmp_path, monkeypatch):
    from hermes_cli import chat_visibility
    from hermes_cli import kanban_comment_writer as ancestry
    monkeypatch.setattr(ancestry, 'agent_ancestor', lambda: '42:LC_HERMES_AGENT')
    monkeypatch.delenv('HERMES_CHAT_VISIBLE', raising=False)
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('proof', 'cli')
        chat_visibility.hide_classic_session(db, 'proof')
        assert db.get_session('proof')['hidden'] == 1
        db.end_session('proof', 'done')
        db.maybe_archive_empty_drafts()
        assert db.get_session('proof')['archived'] == 1
    finally:
        db.close()
