"""Foreign provenance is not an intake-local primary key or a lane exemption."""
import pytest

from hermes_cli import kanban_db as kb, kanban_selfwork_owner as owner, kanban_lanes as lanes
from hermes_cli.kanban_selfwork import SelfWorkError
from tests.hermes_cli import test_kanban_selfwork_creator as creator_support
from tests.hermes_cli import test_kanban_origin_owner as origin_support

home, local, board = creator_support.home, creator_support.local, creator_support.board
context = creator_support.context
world, invoke = origin_support.world, origin_support.invoke


@pytest.mark.parametrize('gated', [False, True])
def test_real_foreign_receiver_create_append_replay(world, tmp_path, monkeypatch, capsys, gated):
    import json
    from hermes_cli import kanban_db_connect as kbc
    if gated:
        lane_policy = lanes.Policy({'hermes': lanes.Lane('hermes', gated=True, self_work=True)},
                                   'hermes', 10)
        monkeypatch.setattr(lanes, 'safe_policy', lambda: lane_policy)
    (tmp_path / 'config.yaml').write_text(json.dumps({'kanban': {'sync': {'enabled': True}, 'self_work': {
        'home': {'gateway': 'owner', 'profile': 'default', 'board': 'default'}}}}))
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_db_sync import set_identity
        set_identity(conn, 'board-owner', 'owner')
        collision = kb.create_task(conn, title='Unrelated', session_id='wrong-chat')
    creator = {'gateway': 'origin', 'profile': 'work', 'board': 'default',
               'task_id': collision, 'run_uid': 'original-run'}
    world['action'] = 'selfwork-create'
    world['args']['creator'] = creator
    world['args']['create'].update(self_work={'version': 1, 'repository': 'hermes-core',
                                            'area': 'transcript', 'kind': 'post-merge'}, requires=[])
    first = invoke(world, monkeypatch, capsys, 'selfwork-create')
    assert first.get('created') is True, first
    assert first['handoff_complete'] is False
    assert first['task_id'] != collision
    assert invoke(world, monkeypatch, capsys, 'selfwork-create')['replayed'] is True
    world['id'] = 'request-owner-002'
    world['args']['create']['title'] = 'More evidence'
    appended = invoke(world, monkeypatch, capsys, 'selfwork-create')
    assert appended.get('disposition') == 'append', appended
    assert appended['task_id'] == first['task_id']
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, first['task_id'])
        assert task.status == ('triage' if gated else 'ready')
        assert task.block_kind == ('lane_gate' if gated else None)
        rows = conn.execute('SELECT request_json FROM self_work_intake').fetchall()
        assert len(rows) == 2
        assert all(json.loads(row[0])['intent']['creator'] == creator for row in rows)
        event = next(e for e in kb.list_events(conn, first['task_id']) if e.kind == 'created')
        assert event.payload['creator_task_id'] is None
        assert not conn.execute("SELECT 1 FROM kanban_notify_subs WHERE task_id=? AND chat_id='wrong-chat'",
                                (first['task_id'],)).fetchone()


def foreign(task_id):
    intent = context(task_id)
    intent.update(origin={'gateway': 'macbook', 'profile': 'default', 'session': 'chat'}, sender='gw:macbook')
    intent['creator']['gateway'] = 'macbook'
    return intent


def test_foreign_creator_never_resolves_colliding_local_task(home):
    task = kb.create_task(home, title='Unrelated local creator', session_id='wrong-chat')
    assert owner.creator_task(home, foreign(task)) is None


@pytest.mark.parametrize('field,value', [('gateway', 'spoof'), ('profile', 'other')])
def test_foreign_creator_must_match_authenticated_origin(home, field, value):
    intent = foreign('t_missing')
    intent['creator'][field] = value
    with pytest.raises(SelfWorkError):
        owner.creator_task(home, intent)


@pytest.mark.parametrize('sender', [None, 'gw:spoof', 'macbook'])
def test_foreign_creator_requires_authenticated_sender(home, sender):
    intent = foreign('t_missing')
    intent['sender'] = sender
    with pytest.raises(SelfWorkError):
        owner.creator_task(home, intent)


def test_foreign_creator_requires_origin(home):
    intent = foreign('t_missing')
    del intent['origin']
    with pytest.raises(SelfWorkError):
        owner.creator_task(home, intent)


@pytest.mark.parametrize('title', ['Fix transcript traffic', 'Review transcript fix'])
def test_courier_selfwork_without_local_creator_is_agent_filed(home, title):
    from types import SimpleNamespace
    import re
    lane = SimpleNamespace(gated=True, creators=(), key_prefixes=(),
                           gate_exempt=(re.compile('^Review'),))
    policy = SimpleNamespace(lanes={'hermes': lane}, lane_of=lambda _: 'hermes')
    opts = {'self_work': {'version': 1, 'repository': 'hermes-core', 'area': 'transport', 'kind': 'post-merge'},
            'created_by': 'gw:macbook', 'title': title, 'creator_task_id': None}
    assert lanes._gated(home, policy, 'hermes', opts)
