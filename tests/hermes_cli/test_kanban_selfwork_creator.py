"""Private courier creator custody must survive both owner admission paths."""
import pytest

from hermes_cli import kanban_db as kb, kanban_selfwork_owner as owner
from hermes_cli.kanban_selfwork import SelfWorkError
from tests.hermes_cli import test_kanban_selfwork_owner as support

home = support.home
local = support.local
board = support.board


def context(task_id):
    return {'owner': {'gateway': 'home', 'profile': 'default', 'board': 'default'},
            'creator': {'gateway': 'home', 'profile': 'default', 'board': 'default',
                        'task_id': task_id, 'run_uid': 'run-creator'}}


def test_resolve_private_creator(home):
    task = kb.create_task(home, title='Creator')
    assert owner.creator_task(home, context(task)) == task


@pytest.mark.parametrize('field,value', [('gateway', 'foreign'), ('profile', 'other'),
                                        ('board', 'other'), ('task_id', 't_missing')])
def test_creator_never_resolves_another_namespace(home, field, value):
    task = kb.create_task(home, title='Creator')
    intent = context(task)
    intent['creator'][field] = value
    with pytest.raises(SelfWorkError):
        owner.creator_task(home, intent)


@pytest.mark.parametrize('gateway', [None, 'home', 'foreign'])
def test_synced_creator_provenance(home, gateway):
    task = kb.create_task(home, title='Creator')
    with kb.write_txn(home):
        home.execute('ALTER TABLE tasks ADD COLUMN created_on TEXT')
        home.execute('UPDATE tasks SET created_on=? WHERE id=?', (gateway, task))
    if gateway == 'foreign':
        with pytest.raises(SelfWorkError):
            owner.creator_task(home, context(task))
    else:
        assert owner.creator_task(home, context(task)) == task


def test_no_creator_keeps_nonworker_compatibility(home):
    assert owner.creator_task(home, {'owner': context('unused')['owner']}) is None


@pytest.mark.parametrize('creator', [None, {}, {'task_id': 't_fake'}, 't_fake'])
def test_malformed_private_creator_refused(home, creator):
    intent = context('t_fake')
    intent['creator'] = creator
    with pytest.raises(SelfWorkError):
        owner.creator_task(home, intent)
