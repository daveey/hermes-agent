"""A transport freshness timeout is not a new owner observation. [daveey]"""
import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as connect
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_remote_terminal_release as records


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(activity, '_remote_last', None)
    monkeypatch.setattr(activity, '_snapshot_saved', None)
    monkeypatch.setattr(activity, '_remote_scope', None)
    now = [records.NOW]
    path = records.replica(tmp_path, [('default/default/t_1', records.observed(records.card('running')))])
    monkeypatch.setattr(activity, '_hub_db_path', lambda: path)
    real_read = activity._read_remote_links
    monkeypatch.setattr(activity, '_read_remote_links', lambda p, **kw: real_read(p, now=now[0], **kw))
    monkeypatch.setattr(activity, '_remote_links', lambda stale_ok=False: activity._legacy_remote_links())
    live = connect.connect(home / 'kanban.db')
    client.ensure_identity(live, 'default', 'origin')
    uid = links.link_uid('t_1', 'origin', 'default', 's1', 'shepherd')

    def run_pass():
        client.reconcile_links([('default', home / 'kanban.db')], 'origin', skip_unchanged=True)
        return live.execute('SELECT released FROM task_chat_links WHERE uid=?', (uid,)).fetchone()[0]

    try:
        yield path, now, run_pass
    finally:
        live.close()
        activity._reset_cache_for_tests()


def test_done_releases_in_one_pass_and_transport_age_cannot_rearm_it(world):
    path, now, run_pass = world
    assert run_pass() == 0
    done = records.observed(records.card('done'))
    done['origin_observation']['verified'] = True
    records.update(path, 'default/default/t_1', done)
    # The consumer has persisted its notice; ack the real delivery ledger before the links pass.
    from tui_gateway.kanban_remote_notices import ack
    assert ack({'session': 's1', 'ref': 'owner/default/default/t_1', 'state': 'done@10000',
                'notice_key': 'terminal-release-test'}, ledger=path.parent / 'notices.db')
    assert run_pass() == 1
    now[0] += activity.HUB_STALE_AFTER_S + 1
    assert run_pass() == 1, 'the same owner-attested completion must not become Waiting again'


def _done(path):
    task = records.observed(records.card('done'))
    task['origin_observation']['verified'] = True
    records.update(path, 'default/default/t_1', task)
    return task


def test_release_survives_restart_from_the_durable_snapshot(world, monkeypatch):
    path, now, run_pass = world
    _done(path)
    assert run_pass() == 1
    monkeypatch.setattr(activity, '_remote_last', None)
    now[0] += activity.HUB_STALE_AFTER_S + 1
    assert run_pass() == 1


@pytest.mark.parametrize('change', ['reopen', 'new completion', 'tampered', 'unverified', 'missing evidence'])
def test_new_owner_record_cannot_borrow_the_previous_release(world, change):
    path, now, run_pass = world
    done = _done(path)
    assert run_pass() == 1
    now[0] += activity.HUB_STALE_AFTER_S + 1
    newer = records.observed(records.card('done', completed_at=12345))
    newer['origin_observation']['verified'] = True
    task = {
        'reopen': records.observed(records.card('running')),
        'new completion': newer,
        'tampered': {**done, 'title': 'modified after signing'},
        'unverified': {**done, 'origin_observation': {**done['origin_observation'], 'verified': False}},
        'missing evidence': {k: v for k, v in done.items() if k != 'origin_observation'},
    }[change]
    records.update(path, 'default/default/t_1', task)
    assert run_pass() == 0


def test_cold_stale_completion_does_not_acquire_a_release(world):
    path, now, run_pass = world
    _done(path)
    now[0] += activity.HUB_STALE_AFTER_S + 1
    assert run_pass() == 0


@pytest.mark.parametrize('field', ['key', 'session', 'gateway', 'profile', 'board', 'id',
                                  'origin_gateway', 'origin_profile', 'via'])
def test_snapshot_release_never_crosses_identity(field):
    from hermes_cli.kanban_terminal_release_snapshot import preserve
    prior = {'key': 'owner/default/default/t_1', 'session': 's1', 'gateway': 'owner', 'profile': 'default',
             'board': 'default', 'id': 't_1', 'origin_gateway': 'origin', 'origin_profile': 'default',
             'via': 'shepherd', '_release_observation': 'a' * 64, 'released': True, 'status': 'done'}
    current = {**prior, field: 'different', 'stale': True, 'status': 'unknown', 'released': False}
    assert preserve([current], [prior]) == [current]


@pytest.mark.parametrize('field,value', [('released', False), ('status', 'running'),
                                        ('_release_observation', None)])
def test_old_or_unreleased_snapshot_is_not_completion_proof(field, value):
    from hermes_cli.kanban_terminal_release_snapshot import preserve
    prior = {'_release_observation': 'a' * 64, 'released': True, 'status': 'done'}
    current = {**prior, 'stale': True, 'status': 'unknown', 'released': False}
    assert preserve([current], [{**prior, field: value}]) == [current]
