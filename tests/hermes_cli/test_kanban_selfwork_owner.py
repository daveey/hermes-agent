"""Real tool normalization -> owner intake -> origin adoption receipts."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork_owner as owner
from tests.hermes_cli import test_kanban_selfwork_entry as entry
from tests.hermes_cli import test_kanban_selfwork_intake as support
from tools import kanban_tools as kt

board = entry.board
local = entry.local
from tests.hermes_cli import test_kanban_origin_owner as courier_support
world = courier_support.world


def test_real_courier_cli_create_append_and_replay(world, tmp_path, monkeypatch, capsys):
    (tmp_path / 'config.yaml').write_text(json.dumps({'kanban': {'self_work': {
        'home': {'gateway': 'owner', 'profile': 'default', 'board': 'default'}}}}), encoding='utf-8')
    world['args']['create'].update(self_work=entry.support.declaration(), requires=[])
    first = courier_support.invoke(world, monkeypatch, capsys)
    assert first.get('created') is True, first
    world['id'] = 'request-owner-002'
    world['args']['create']['title'] = 'Another finding'
    second = courier_support.invoke(world, monkeypatch, capsys)
    assert second.get('created') is False, second
    assert second['task_id'] == first['task_id']
    assert second['origin_creation']['request_id'] == first['origin_creation']['request_id']
    replay = courier_support.invoke(world, monkeypatch, capsys)
    assert replay.get('replayed') is True, replay



@pytest.fixture
def home(local, monkeypatch):
    root = kb.kanban_home().resolve()
    config = {'kanban': {'self_work': {'home': {'gateway': 'home', 'profile': 'default', 'board': 'default'}}}}
    monkeypatch.setattr(owner, '_runtime', lambda: (config, 'home', 'default', root))
    monkeypatch.setattr(kt, '_landing_placement', lambda conn, args, origin, intent, marker, runs, **kw: (None, runs))
    return local


def create(request='owner-request-001', *, title='Fix', **extra):
    args = {'title': title, 'self_work': entry.support.declaration(), 'board': 'default',
            'requires': [], 'session_id': 'origin-session', **extra}
    return json.loads(kt._handle_create(args, origin_intent=support.origin(request)))


def test_create_append_replay_and_actual_origin_receipt(home):
    first = create()
    assert first['created'] is True, first
    assert first['handoff_complete'] is False
    second = create('owner-request-002', title='New evidence')
    assert second['created'] is False, second
    assert second['task_id'] == first['task_id']
    assert second['disposition'] == 'append'
    assert second['origin_creation']['request_id'] == first['origin_creation']['request_id']
    assert create('owner-request-002', title='New evidence')['replayed'] is True
    assert len(kb.list_tasks(home)) == 1
    assert len(kb.list_comments(home, first['task_id'])) == 1


@pytest.mark.parametrize('parents', [None, 'parent'])
def test_owner_normalizes_parent_inputs(home, parents):
    parent = kb.create_task(home, title='Parent')
    value = parent if parents else None
    first = create(parents=value)
    assert first.get('created') is True, first
    second = create('owner-request-002', parents=value)
    assert second.get('disposition') == 'append', second


def test_concurrent_replay_rechecks_under_append_lock(home, monkeypatch):
    from hermes_cli import kanban_selfwork_intake as intake
    first = create()
    with kb.write_txn(home):
        home.execute("UPDATE tasks SET status='done' WHERE id=?", (first['task_id'],))
    original = intake.replay
    calls = []
    def stale_once(*args):
        calls.append(1)
        return None if len(calls) == 1 else original(*args)
    monkeypatch.setattr(intake, 'replay', stale_once)
    def fail(*args, **kwargs):
        pytest.fail('concurrent committed replay reached preflight')
    monkeypatch.setattr(kt, '_landing_placement', fail)
    assert create()['replayed'] is True


def test_append_precedes_changed_creation_preflight(home, monkeypatch):
    first = create()
    assert first['created'] is True, first
    def fail(*args, **kwargs):
        pytest.fail('append ran creation-only preflight')
    monkeypatch.setattr(kt, '_landing_placement', fail)
    result = create('owner-request-002', title='Additional evidence')
    assert result['disposition'] == 'append', result
    assert result['task_id'] == first['task_id']
    assert len(kb.list_comments(home, first['task_id'])) == 1


def test_replay_precedes_changed_creation_preflight(home, monkeypatch):
    first = create()
    assert first['created'] is True, first
    def fail(*args, **kwargs):
        pytest.fail('replay ran creation-only preflight')
    monkeypatch.setattr(kt, '_landing_placement', fail)
    replay = create()
    assert replay['replayed'] is True
    assert replay['task_id'] == first['task_id']


def test_replay_compares_raw_request_not_derived_options(home, monkeypatch):
    first = create()
    assert first['created'] is True, first
    monkeypatch.setattr(kt, '_persisted_identity', lambda: 'changed-runtime-default')
    replay = create()
    assert replay.get('replayed') is True, replay
    assert replay['task_id'] == first['task_id']
    conflict = create(title='Changed immutable input')
    assert not conflict.get('replayed'), conflict
    assert 'different intent' in conflict.get('error', '')
    assert len(kb.list_tasks(home)) == 1


def test_replay_revalidates_home(home, monkeypatch):
    first = create()
    assert first['created'] is True, first
    config, _, profile, root = owner._runtime()
    monkeypatch.setattr(owner, '_runtime', lambda: (config, 'foreign', profile, root))
    replay = create()
    assert not replay.get('replayed'), replay
    assert 'mismatch' in replay.get('error', '')


def test_raw_receipt_is_atomic_with_task(home, monkeypatch):
    from hermes_cli import kanban_selfwork_intake as intake
    original = intake._store
    def fail(conn, *args):
        original(conn, *args)
        raise RuntimeError('receipt failed')
    monkeypatch.setattr(intake, '_store', fail)
    result = create()
    assert 'receipt failed' in result.get('error', ''), result
    assert not kb.list_tasks(home)
    monkeypatch.setattr(intake, '_store', original)
    assert create()['created'] is True


def test_closed_owner_creates_new_card(home):
    first = create()
    assert first['created'] is True, first
    with kb.write_txn(home):
        home.execute("UPDATE tasks SET status='done' WHERE id=?", (first['task_id'],))
    result = create('owner-request-002', title='New work', session_id='second-origin-session')
    assert result['task_id'] != first['task_id'], result
    assert result['created'] is True
    assert len(kb.list_tasks(home)) == 2


@pytest.mark.parametrize('field,value', [('gateway', 'other'), ('profile', 'other'), ('board', 'other')])
def test_wrong_home_tuple_never_creates(home, field, value):
    intent = support.origin('owner-request-001')
    intent['owner'][field] = value
    result = json.loads(kt._handle_create({'title': 'Fix', 'self_work': entry.support.declaration(),
                                         'board': 'default', 'requires': []}, origin_intent=intent))
    assert not result.get('created'), result
    assert not kb.list_tasks(home)


def test_public_tool_cannot_forge_runtime_origin(home):
    result = json.loads(kt._handle_create({'title': 'Fix', 'self_work': entry.support.declaration(),
                                         'board': 'default', 'origin_intent': support.origin('owner-request-001')}))
    assert not result.get('created'), result
    assert not kb.list_tasks(home)


@pytest.mark.parametrize('config', [None, {}, {'kanban': []}, {'kanban': {'self_work': []}},
                                    {'kanban': {'self_work': {'home': {}}}}])
def test_missing_or_malformed_home_is_fail_closed(home, monkeypatch, config):
    monkeypatch.setattr(owner, '_runtime', lambda: (config, 'home', 'default', kb.kanban_home().resolve()))
    with pytest.raises(ValueError, match='configure'):
        owner.validate_home(home, support.origin('owner-request-001'), 'default')
    assert not kb.list_tasks(home)


@pytest.mark.parametrize('mismatch', ['gateway', 'profile', 'root', 'connection'])
def test_runtime_identity_and_physical_database_are_checked(home, monkeypatch, tmp_path, mismatch):
    import sqlite3
    config, gateway, profile, root = owner._runtime()
    if mismatch == 'gateway':
        gateway = 'foreign'
    elif mismatch == 'profile':
        profile = 'foreign'
    elif mismatch == 'root':
        root = tmp_path / 'foreign'
    monkeypatch.setattr(owner, '_runtime', lambda: (config, gateway, profile, root))
    foreign = sqlite3.connect(tmp_path / 'foreign.db')
    try:
        with pytest.raises(ValueError, match='mismatch'):
            owner.validate_home(foreign if mismatch == 'connection' else home,
                                support.origin('owner-request-001'), 'default')
    finally:
        foreign.close()
    assert not kb.list_tasks(home)
