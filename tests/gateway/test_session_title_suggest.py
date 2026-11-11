"""Rename pre-fill is served by the session owner, including its profile secrets."""
from types import SimpleNamespace

import pytest

from tests.waiting import async_wait_until


@pytest.mark.parametrize('key', ['session_key', 'session_id'])
@pytest.mark.parametrize('title', ['owner title', None])
@pytest.mark.asyncio
async def test_title_suggestion_uses_owner_transcript_and_secrets(tmp_path, monkeypatch, key, title):
    from agent import secret_scope
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from hermes_constants import get_hermes_home
    import hermes_state_runtime as rt

    root = tmp_path / 'home'
    home = root / 'profiles' / 'helper'
    home.mkdir(parents=True)
    (root / '.env').write_text('TITLE_PROBE_KEY=wrong-profile\n')
    (home / '.env').write_text('TITLE_PROBE_KEY=owner-secret\n')
    (home / 'config.yaml').write_text('{}\n')
    monkeypatch.setenv('HERMES_HOME', str(root))
    store = SessionStore(home / 'sessions', GatewayConfig())
    db = store._db
    runner = SimpleNamespace(config=GatewayConfig(multiplex_profiles=True),
                             session_store=store, _session_db=db, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    authority = SessionAuthority(runner, profile_id=str(home), instance_id='owner', db=db,
                                 epoch=rt.begin_runtime_epoch(db, instance_id='owner'))
    frames = []

    async def write(frame):
        frames.append(frame)
        return True

    connection = AuthorityConnection(authority, SimpleNamespace(write_async=write), {'user_id': 'human'})
    seen = []

    def suggest(messages):
        seen.append((get_hermes_home(), secret_scope.get_secret('TITLE_PROBE_KEY'), messages))
        return title

    monkeypatch.setattr('agent.title_generator.suggest_short_name', suggest)
    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        from gateway.session_authorities import owner_scope
        with owner_scope(authority):
            ref = create_local_session(authority, connection.actor,
                                       dict(request_id='title', source='gui', cwd=str(tmp_path), model='test-model', toolsets=[]))
        db.append_message(ref.session_id, 'user', 'owner transcript')
        before = db.get_session(ref.session_id)
        result = await connection.dispatch({'id': 1, 'method': 'session.title.suggest',
                                            'params': {key: ref.session_id, 'profile': 'helper'}})
        assert result is None, result
        await async_wait_until(lambda: bool(frames))
        assert frames[0]['result'] == {'session_key': ref.session_id, 'title': title}
        assert seen[0][:2] == (home, 'owner-secret')
        assert any(message['content'] == 'owner transcript' for message in seen[0][2])
        assert db.get_session(ref.session_id)['title'] == before['title']
        for params, reason in (({}, 'invalid_params'), ({key: 'missing'}, 'not_found')):
            refused = await connection.dispatch({'id': 2, 'method': 'session.title.suggest', 'params': params})
            assert refused['error']['message'] == reason
        from dataclasses import replace
        connection.actor = replace(connection.actor, capabilities=frozenset())
        refused = await connection.dispatch({'id': 3, 'method': 'session.title.suggest',
                                             'params': {key: ref.session_id}})
        assert refused['error']['message'] == 'permission_denied'
        assert len(seen) == 1
    finally:
        await connection.close()
        store.close_all_db_handles()
        secret_scope.set_multiplex_active(previous)
