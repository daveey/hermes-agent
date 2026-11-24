"""Agent CLI visibility is frozen at creation, not inferred from the gateway's environment."""
import json
import os

import pytest

from tests.gateway.test_session_row_cwd import _authority_fixture


class _PidScopedOs:
    """The real ``os`` for every attribute except a fixed fake ``getpid``; assigned to one module only."""

    @staticmethod
    def getpid():
        return 999

    def __getattr__(self, name):
        return getattr(os, name)


@pytest.mark.parametrize('marker,visible', [('LC_HERMES_AGENT', False), ('HERMES_AGENT', False),
                                          (None, False), ('HERMES_AGENT', True)])
@pytest.mark.asyncio
async def test_agent_hidden_create_is_atomic_and_retry_bound(tmp_path, monkeypatch, marker, visible):
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from hermes_state_runtime import RuntimeStoreError
    from hermes_cli import kanban_comment_writer as ancestry
    from hermes_cli.chat_visibility import creation_visibility
    # Scope the fake pid to the ancestry module. Patching ``ancestry.os.getpid`` rewrites the process-wide
    # ``os`` module, so the first import of ``gateway.run`` below (whose pre-import launch enrollment reads
    # this process's own /proc entry) observed pid 999 and failed on /proc/999/stat.
    monkeypatch.setattr(ancestry, 'os', _PidScopedOs())
    assert os.getpid() != 999 and ancestry.os.getpid() == 999  # fake pid is module-scoped, not process-wide
    monkeypatch.setattr(ancestry, '_read_proc', lambda pid: (998, {}) if pid == 999 else
                        (0, {marker: 'parent'} if marker else {}))
    monkeypatch.delenv('HERMES_CHAT_VISIBLE', raising=False)

    store, runner = _authority_fixture(tmp_path, monkeypatch)
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='owner')
    actor = AuthorityConnection(authority, object(), {'user_id': 'human', 'profile_id': 'fixture'}).actor
    params = dict(request_id='agent', source='cli', cwd=str(tmp_path), model='m', toolsets=[],
                  **creation_visibility(visible=visible))
    try:
        ref = create_local_session(authority, actor, params)
        row = authority.db.get_session(ref.session_id)
        assert bool(row['hidden']) is (marker is not None and not visible)
        assert json.loads(row['model_config'] or '{}').get('_agent_cli', False) is (marker is not None)
        assert create_local_session(authority, actor, params).session_id == ref.session_id
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            create_local_session(authority, actor, {**params, 'hidden': not params['hidden']})
    finally:
        store.close_all_db_handles()
