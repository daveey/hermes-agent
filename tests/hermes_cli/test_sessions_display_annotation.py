"""The single-profile HTTP list uses the same bounded display enrichment as the merged list."""
from hermes_cli.web_routers import sessions
from hermes_state import SessionDB


def test_session_payload_uses_display_annotation(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = SessionDB(home / 'state.db')
    db.create_session('visible', source='cli')
    db.append_message('visible', 'user', 'hello')
    called = []
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', lambda *a, **kw: db)
    monkeypatch.setattr(sessions, 'annotate_kanban_activity', lambda rows, **kw: called.append(kw))
    try:
        result = sessions._list_sessions_payload(20, 0, 0, 'exclude', 'recent', None, None, None,
                                                  None, False, 'include', None)
        assert result['sessions'][0]['id'] == 'visible'
        assert called == [{'display': True}]
    finally:
        db.close()
