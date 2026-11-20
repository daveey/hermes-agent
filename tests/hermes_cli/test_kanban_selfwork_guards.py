"""Guard freezing acts only at intake, before generic area batching."""
import logging

import pytest

from hermes_cli import config, kanban_db as kb, kanban_selfwork as policy
from hermes_cli import kanban_selfwork_intake as intake
from tests.hermes_cli import test_kanban_selfwork as support

board = support.board


@pytest.fixture
def settings(monkeypatch):
    value = {'kanban': {'self_work': {}}}
    monkeypatch.setattr(config, 'load_config', lambda: value)
    return value['kanban']['self_work']


def admit(conn, request='guard-request-001', **options):
    return intake.admit(conn, request_id=request, authority='home', local_gateway='home', now=1794690000,
                        options={'title': 'Guard finding', 'self_work': support.declaration('guard'), **options})


def seed(conn, **changes):
    tid = kb.create_task(conn, title='Existing owner', tenant=changes.pop('tenant', None))
    policy.record(conn, tid, support.declaration(changes.pop('kind', 'guard'), **changes), day='2020-01-01')
    return tid


def test_default_freeze_refuses_without_owner(board, settings, caplog):
    with caplog.at_level(logging.INFO), pytest.raises(policy.SelfWorkError, match='freeze_guards'):
        admit(board)
    assert not kb.list_tasks(board)
    assert len([r for r in caplog.records if 'guard creation frozen' in r.message]) == 1


def test_append_old_guard_and_replay_once(board, settings, caplog):
    tid = seed(board)
    before = kb.get_task(board, tid)
    with caplog.at_level(logging.INFO):
        result = admit(board)
        replay = admit(board)
    assert result['disposition'] == 'append' and result['task_id'] == tid
    assert replay['replayed'] is True
    assert len(kb.list_comments(board, tid)) == 1
    assert kb.get_task(board, tid).status == before.status
    assert len([r for r in caplog.records if 'guard creation frozen' in r.message]) == 1


@pytest.mark.parametrize('change', [{'kind': 'sweep'}, {'area': 'other'}, {'repository': 'hermes-state'},
                                     {'tenant': 'other'}])
def test_wrong_owner_cannot_receive_guard(board, settings, change):
    seed(board, **change)
    with pytest.raises(policy.SelfWorkError, match='freeze_guards'):
        admit(board)
    assert len(kb.list_tasks(board)) == 1


@pytest.mark.parametrize('status', ['done', 'archived'])
def test_closed_guards_not_reused(board, settings, status):
    tid = seed(board)
    board.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
    board.commit()
    with pytest.raises(policy.SelfWorkError, match='freeze_guards'):
        admit(board)


def test_off_allows_create_and_existing_replay_after_freeze(board, settings):
    settings['freeze_guards'] = False
    first = admit(board)
    assert first['created'] is True
    settings['freeze_guards'] = True
    assert admit(board)['replayed'] is True


@pytest.mark.parametrize('kind', sorted(policy.KINDS - {'guard'}))
def test_other_kinds_unchanged(board, settings, kind):
    assert admit(board, self_work=support.declaration(kind))['created'] is True


def test_product_unchanged(board, settings):
    tid = kb.create_task(board, title='Product lifecycle guard')
    assert policy.declared(board, tid) is None


@pytest.mark.parametrize('owner', [False, True])
def test_append_probe_agrees(board, settings, owner):
    tid = seed(board) if owner else None
    options = {'title': 'Guard finding', 'self_work': support.declaration('guard')}
    def call():
        return intake.append_existing(board, request_id='guard-probe-001', authority='home',
                                      options=options, request=options, now=1794690000)
    if owner:
        assert call()['task_id'] == tid
    else:
        with pytest.raises(policy.SelfWorkError, match='freeze_guards'):
            call()


@pytest.mark.parametrize('bad', [None, 0, 'false', [], {}])
def test_malformed_switch_does_not_unfreeze(board, settings, bad):
    settings['freeze_guards'] = bad
    with pytest.raises(policy.SelfWorkError, match='freeze_guards'):
        admit(board)


def test_oldest_open_guard_owner_receives(board, settings):
    older, newer = seed(board), seed(board)
    board.execute('UPDATE tasks SET created_at=100 WHERE id=?', (older,))
    board.execute('UPDATE tasks SET created_at=200 WHERE id=?', (newer,))
    board.commit()
    assert admit(board)['task_id'] == older
