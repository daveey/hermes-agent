"""[daveey] ``hermes chat --source <tag>``: a free integration tag still starts a session.

Scripts tag their one-shots (``bb-reviewer``, ``bb-judge``, ``peer-mail``, ``selfheal-replay``) so
``sessions.sidebar_exclude_sources`` and ``hermes sessions list --source`` can name them. Once the CLI
created its sessions through ``session.create``, the closed create contract refused every such tag with
``invalid_params (source)``, so the BB merge queue's reviewer and every hermes-mail prompt failed before
reaching a model. A free tag runs as the ``tool`` surface and keeps its own label on the row.
"""
import json

import pytest

from tests.gateway.test_resume_latest_local_cli import _call, _listed, _owner


@pytest.mark.asyncio
@pytest.mark.parametrize('tag', ['bb-reviewer', 'peer-mail', 'selfheal.replay_2'])
async def test_a_free_tag_creates_a_tool_surface_session_stored_under_its_own_label(tmp_path, monkeypatch, capsys, tag):
    store, authority, owner = _owner(tmp_path / 'home', monkeypatch)
    try:
        created = await _call(owner, 'session.create', request_id='r', source=tag, cwd=str(tmp_path), toolsets=[],
                              model='m')
        assert isinstance(created, dict), created
        sid = created['session_id']
        row = authority.db.get_session(sid)
        assert (row['source'], row['created_source']) == (tag, tag)
        from gateway.session_policy import restore_policy
        from hermes_state_local import local_receipt
        policy = restore_policy(local_receipt(authority.db, sid)['policy'])
        assert (policy.source, policy.platform) == (tag, 'cli')
        assert sid in _listed(authority.db, tag, capsys)
        assert sid not in _listed(authority.db, 'cli', capsys)
        # The peer refresh keeps the label (only cli/tui/gui/acp rows are relabelled local).
        live = authority.sessions[sid]
        store._record_gateway_session_peer(sid, live.route, live.source)
        assert authority.db.get_session(sid)['source'] == tag
    finally:
        await owner.close()
        store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize('tag', ['telegram', 'cron', 'kanban', 'local', 'desktop', 'a2a', 'Bad Tag', '', 'x' * 65])
async def test_platform_names_routing_labels_and_malformed_tags_are_still_refused(tmp_path, monkeypatch, tag):
    store, _authority, owner = _owner(tmp_path / 'home', monkeypatch)
    try:
        refused = await _call(owner, 'session.create', request_id='r', source=tag, cwd=str(tmp_path), toolsets=[],
                              model='m')
        assert refused == 'invalid_params'
    finally:
        await owner.close()
        store.close_all_db_handles()


def test_a_free_tag_policy_survives_a_restore(tmp_path):
    from dataclasses import asdict
    from gateway.session_policy import build_policy, restore_policy
    policy = build_policy({'source': 'bb-judge', 'cwd': str(tmp_path), 'toolsets': [], 'model': 'm'},
                          {'model': {'default': 'm'}}, profile_terminal=False)
    restored = restore_policy(json.loads(json.dumps(asdict(policy))))
    assert (restored.source, restored.platform) == ('bb-judge', 'cli')


def test_the_create_contract_accepts_a_free_tag_and_refuses_a_malformed_one():
    from tui_gateway.contracts.canonical_sessions import CanonicalCreateParams
    assert CanonicalCreateParams.model_validate({'source': 'bb-reviewer'}).source == 'bb-reviewer'
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CanonicalCreateParams.model_validate({'source': 'Bad Tag'})
