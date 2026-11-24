"""Real startup owns seven native sources, with no legacy annotation admission."""
import asyncio
import time
import pytest

from hermes_cli import fact_annotation_boot as boot
from hermes_cli import fact_annotation_lifespan as startup
from hermes_cli.kanban_annotation_facts import AnnotationUnavailable
from hermes_cli.fact_annotation_startup import binding_for
from tests.hermes_cli import test_kanban_session_activity as helpers
from tests.hermes_cli.annotation_fact_processes import request_guard
from tests.waiting import wait_until

kanban_root = helpers.kanban_root


def test_real_prepare_lifespan_and_canonical_request(kanban_root, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_state import SessionDB
    from hermes_cli import profiles
    from hermes_cli import kanban_session_activity as kanban
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli.web_routers import profiles as routes
    home = tmp_path / 'native-profile'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('seed', 'cli')
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '1')
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(routes, '_profile_targets', lambda reason: [('default', home)])
    monkeypatch.setattr(boot, 'seeds', lambda *a, **k: pytest.fail('prepare must not scan sessions'))
    monkeypatch.setattr(kanban, '_remote_cards', lambda *args: [])
    calls = []
    monkeypatch.setattr(host, 'suspend_holder', lambda: calls.append('drain'))
    monkeypatch.setattr(host, 'resume_holder', lambda role: calls.append('resume'))
    async def run():
        app = SimpleNamespace(state=SimpleNamespace())
        async with startup.lifespan(app):
            assert binding_for() is app.state.annotation_facts.bindings['default']
            assert calls == []  # lazy boot: no activation, legacy untouched
            with request_guard():
                row = {'id': 'seed'}
                with pytest.raises(AnnotationUnavailable):  # fail closed, never legacy
                    kanban.annotate_kanban_activity([row], display=True)
            assert not row.get('kanban_active')
            runtime = app.state.annotation_facts
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with request_guard():
                        kanban.annotate_kanban_activity([{'id': 'seed'}], display=True)
                    break
                except AnnotationUnavailable:
                    await asyncio.sleep(0.1)
            assert runtime.bindings['default']._mode == 'facts'
            assert calls == ['drain']
        assert calls == ['drain', 'resume']
        assert binding_for() is None
    try:
        asyncio.run(run())
    finally:
        db.close()


@pytest.mark.parametrize('optional', ['healthy', 'unused-profile', 'no-pr-info'])
def test_real_serve_composes_annotations_outside_sessions(kanban_root, tmp_path, monkeypatch, optional):
    from contextlib import asynccontextmanager
    from functools import partial
    from hermes_cli import sessions_native_startup as sessions_startup
    from hermes_cli.sessions_capture_worker import capture
    from fastapi.testclient import TestClient

    # Preserve this composition test's explicit roster and annotation doubles;
    # fresh-process real-home capture is covered by test_sessions_capture_process.
    monkeypatch.setattr(sessions_startup, 'SessionsRuntime',
                        partial(sessions_startup.SessionsRuntime, capture_reader=capture))
    from hermes_state import SessionDB
    from hermes_cli import profiles, web_server, web_server_app
    from hermes_cli import kanban_session_activity as kanban
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli.web_routers import profiles as routes
    home = tmp_path / 'composed'
    home.mkdir()
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('composed-row', 'cli')
    db.set_session_title('composed-row', 'real composition')
    roster = [('default', home)]
    if optional == 'unused-profile':
        unused = tmp_path / 'unused'
        unused.mkdir()
        (unused / 'config.yaml').write_text('{}')
        roster.append(('unused', unused))
    if optional == 'no-pr-info':
        from hermes_cli import review_session_activity as review
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('composed-row','assistant',?,1)",
            ('https://github.com/review-fixture/private/pull/7',)))
        monkeypatch.setattr(review, '_fetch_override', lambda key: None)
        monkeypatch.setattr(review, '_sync_fetch', True)
        monkeypatch.setattr(review, '_pr_cache', {})
        monkeypatch.setattr(review, '_inflight', set())
    @asynccontextmanager
    async def quiet_services(app):
        yield
    monkeypatch.setattr(web_server_app, 'standalone_lifespan', quiet_services)
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(routes, '_profile_targets', lambda reason: roster)
    monkeypatch.setattr(kanban, '_remote_cards', lambda *args: [])
    calls = []
    monkeypatch.setattr(host, 'suspend_holder', lambda: calls.append('drain'))
    def resume(role):
        assert web_server.app.state.sessions_facts is None
        calls.append('resume')
    monkeypatch.setattr(host, 'resume_holder', resume)
    monkeypatch.setenv('HERMES_FACTS_LINKS', '0')
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '1')
    monkeypatch.setenv('HERMES_FACTS_SESSIONS', '1')
    try:
        with TestClient(web_server.app) as client:
            client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
            assert binding_for() is not None
            assert wait_until(lambda: web_server.app.state.sessions_facts.ready, timeout=10)
            assert web_server.app.state.sessions_facts.binding._mode == 'facts'
            response = client.get('/api/profiles/sessions')
            assert response.status_code == 200
            assert any(row['id'] == 'composed-row' for row in response.json()['sessions'])
        assert calls == ['drain', 'resume']
    finally:
        db.close()
