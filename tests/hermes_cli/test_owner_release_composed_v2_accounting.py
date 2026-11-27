"""V2 real CLI/DO/HOME accounting and historical recovery, no HOME signer.

Only capability READs are overlaid; all writes use the enrolled loopback Board.
Usage below is a labelled fixture observation on the real retained run, not a
claim that a model spent tokens. No live Board or production root is touched.
"""
import json
import uuid

import pytest

from hermes_cli import kanban_release_final as final, kanban_spend_budget as budget
from hermes_cli import kanban_release_final_home as final_home
from tests.hermes_cli import test_owner_release_composed as c
from tests.hermes_cli import test_owner_release_composed_controls as controls
from tests.hermes_cli import test_owner_release_composed_holds as holds

turn = c.turn
server = c.server
origin = c.origin
parties = c.parties


@pytest.fixture(autouse=True)
def ordinary_v2(monkeypatch):
    capability = c.ksc.Hub.decision_retain_capability
    monkeypatch.setattr(c.ksc.Hub, 'decision_retain_capability',
                        lambda self, board: {**capability(self, board), 'owner_release_application': 2})
    # Existing control helpers call install_allowance. In THIS module the setup
    # explicitly stays ungoverned: never call a signer or spend-window endpoint.
    monkeypatch.setattr(c, 'install_allowance', lambda *args: None)
    actual = c.ksc.Hub._call
    def no_windows(self, method, path, *args, **kwargs):
        assert '/spend-windows' not in path, 'ordinary fixture attempted governance provisioning'
        return actual(self, method, path, *args, **kwargs)
    monkeypatch.setattr(c.ksc.Hub, '_call', no_windows)


def prior_budget(home, task, episode, writer, unlimited, reached):
    stop = home.hub.row_evidence(c.BOARD, 'task_events', episode)['row']
    value = {'runs': 0 if unlimited else 10, 'cache_read_tokens': 50 if reached else 1000,
             'writer_kind': writer, 'reason': 'prior fixture decision', 'by': 'prior'}
    if writer == 'unknown':
        value.pop('writer_kind')
    controls.put(home, 'task_events', 'prior-budget-' + uuid.uuid4().hex,
                 {'task_id': task, 'kind': 'spend_budget_set', 'payload': json.dumps(value),
                  'created_at': stop['created_at'] - 1})
    controls.put(home, 'task_events', 'usage-' + uuid.uuid4().hex,
                 {'task_id': task, 'run_uid': stop['run_uid'], 'kind': 'worker_result',
                  'payload': json.dumps({'usage': {'cache_read_tokens': 100}}),
                  'created_at': stop['created_at']})
    home.resync()
    spent = budget.spend(home.conn, task)
    assert spent.runs == 1 and spent.reported == 1 and spent.tokens['cache_read_tokens'] == 100


@pytest.mark.parametrize('writer,unlimited,reached', [
    ('agent', False, False), ('agent', False, True), ('agent', True, False), ('agent', True, True),
    ('human', False, False), ('unknown', False, False)])
def test_real_ordinary_accounting_and_cache_parity(origin, parties, monkeypatch, writer, unlimited, reached):
    seed = c.seed_stopped_task
    def setup(home, ident, session):
        task, episode = seed(home, ident, session)
        prior_budget(home, task, episode, writer, unlimited, reached)
        return task, episode
    monkeypatch.setattr(c, 'seed_stopped_task', setup)
    home, executor, native, task, episode, uid, _ = c.authorize_and_drive_home(
        origin, parties, monkeypatch, governed=False)
    cert = home.hub.owner_release_application(c.BOARD, uid)
    p = cert['operation']['proposal']
    assert cert['version'] == p['version'] == 2 and cert['governance'] == {'state': 'ungoverned'}
    assert p['accounting'] == {'semantics': 'core_home_ordinary_v1', 'spent_runs': 1}
    events = [json.loads(e['payload']) for e in p['events'] if e['kind'] == 'spend_budget_set']
    if unlimited and not reached:
        assert events == []
    else:
        assert len(events) == 1 and events[0]['runs'] == (0 if unlimited else 2)
        assert events[0]['writer_kind'] == 'agent'
        assert events[0]['cache_read_tokens'] == (0 if writer == 'agent' else 1000)
        assert events[0]['cache_read_writer_kind'] == writer
    assert [r['episode'] for r in c.release_receipts(home, task)] == [episode]
    executor.resync()
    from hermes_cli import kanban_spend_decision as decision
    assert not decision.execution_restricted(executor.conn, task)
    result = c.release_cli(native, task, episode)
    assert result.returncode == 0 and json.loads(result.stdout)['release_stage'] == 'applied'


@pytest.mark.parametrize('writer', ['human', 'unknown'])
def test_real_protected_reached_cache_refuses_without_effects(origin, parties, monkeypatch, caplog, writer):
    from tests.tools import test_kanban_worker_created_chats as chats
    home, executor = parties
    ident, native = origin
    task, episode = c.seed_stopped_task(home, ident, chats.CHAT)
    prior_budget(home, task, episode, writer, False, True)
    uid = controls.publish(native, task, episode)
    controls.assert_held(home, executor, task, uid, monkeypatch, state='consumed')
    assert final.saved(home.conn, uid) is None
    delivery = home.conn.execute('SELECT delivery_uid FROM owner_release_home_queue WHERE intent_uid=?', (uid,)).fetchone()[0]
    assert any(delivery in r.getMessage() and 'cache-read limit' in r.getMessage() for r in caplog.records)


def test_real_refused_budget_does_not_starve_healthy_sibling(origin, parties, monkeypatch):
    from tests.tools import test_kanban_worker_created_chats as chats
    home, _ = parties
    ident, native = origin
    held, held_episode = c.seed_stopped_task(home, ident, chats.CHAT)
    prior_budget(home, held, held_episode, 'human', False, True)
    held_uid = controls.publish(native, held, held_episode)
    healthy, episode = c.seed_stopped_task(home, ident, chats.CHAT)
    uid = controls.publish(native, healthy, episode)
    home.resync()
    for _ in range(80):
        c.home_tick(home, monkeypatch)
        if c.applied(home, uid):
            break
    assert c.applied(home, uid) and not c.applied(home, held_uid)
    assert c.release_receipts(home, held) == []
    assert [r['episode'] for r in c.release_receipts(home, healthy)] == [episode]
    held_row = home.conn.execute('SELECT stage,checked FROM owner_release_home_queue WHERE intent_uid=?', (held_uid,)).fetchone()
    assert held_row['stage'] == 'consumed_pending_apply' and held_row['checked'] > 0
    assert home.hub.owner_release_application(c.BOARD, uid)['governance'] == {'state': 'ungoverned'}


def test_real_v2_omitted_history_remains_held(origin, parties, monkeypatch, caplog):
    c.test_omitted_historical_capability_remains_held(origin, parties, monkeypatch, caplog)


@pytest.mark.parametrize('mode', ['comments', 'task', 'dependency'])
def test_real_v2_freshness_and_dependency(origin, parties, monkeypatch, caplog, mode):
    controls.test_real_postpublication_current_gates_refuse_before_effects(origin, parties, monkeypatch, caplog, mode)


@pytest.mark.parametrize('hold', ['safety', 'containment'])
def test_real_v2_independent_hold_ingress(origin, parties, monkeypatch, hold):
    holds.test_real_current_independent_hold_refuses_native_release(origin, parties, monkeypatch, hold)


@pytest.mark.parametrize('safety', [False, True])
def test_real_v2_successor_historical_replay(origin, parties, monkeypatch, safety):
    controls.test_real_historical_native_replay_never_renews_successor(origin, parties, monkeypatch, safety)


def test_real_v2_lost_post_reply_recovers_after_capability_withdrawal(origin, parties, monkeypatch):
    from tests.tools import test_kanban_worker_created_chats as chats
    from hermes_cli import kanban_origin
    home, _ = parties
    ident, native = origin
    task, episode = c.seed_stopped_task(home, ident, chats.CHAT)
    uid = controls.publish(native, task, episode)
    actual_ops = c.ksc.Hub.ops
    actual_read = c.ksc.Hub.owner_release_application
    posts = []
    reads = []
    lost = []
    def observe_ops(hub, board, ops, *args, **kwargs):
        result = actual_ops(hub, board, ops, *args, **kwargs)
        if any(op.get('op') == 'apply_owner_release' and op.get('intent_uid') == uid for op in ops):
            posts.extend(ops)
            lost.append(True)
            raise OSError('fixture lost reply AFTER real DO commit')
        return result
    def lose_recovery_read(hub, board, intent):
        if intent == uid and lost:
            reads.append(intent)
            raise OSError('fixture first reconciliation unavailable')
        return actual_read(hub, board, intent)
    monkeypatch.setattr(c.ksc.Hub, 'ops', observe_ops)
    monkeypatch.setattr(c.ksc.Hub, 'owner_release_application', lose_recovery_read)
    home.resync()
    for _ in range(80):
        c.home_tick(home, monkeypatch)
        if lost:
            break
    assert len(posts) == 1 and reads and not c.applied(home, uid)
    cert = actual_read(home.hub, c.BOARD, uid)
    assert cert['version'] == 2 and cert['governance'] == {'state': 'ungoverned'}
    before = c.event_inventory(home, task)
    wire = final.saved(home.conn, uid)
    assert wire == posts[0]
    # Restore only the READ, and remove mutable capability before local recovery.
    monkeypatch.setattr(c.ksc.Hub, 'owner_release_application', actual_read)
    monkeypatch.setattr(c.ksc.Hub, 'decision_retain_capability', lambda *a: {})
    row = home.conn.execute('SELECT * FROM owner_release_home_queue WHERE intent_uid=?', (uid,)).fetchone()
    with monkeypatch.context() as scoped:
        scoped.setattr(c.ksc, 'load_identity', lambda *a, **k: home.ident)
        scoped.setattr(kanban_origin, 'gateway_names', lambda: (home.name, {home.name}))
        final_home.process(home.conn, home.hub, row, home.name, c.BOARD)
    assert c.applied(home, uid) and len(posts) == 1
    assert c.event_inventory(home, task) == before
    assert final.saved(home.conn, uid) == wire
    result = c.release_cli(native, task, episode)
    assert result.returncode == 0 and json.loads(result.stdout)['release_stage'] == 'applied'
    assert actual_read(home.hub, c.BOARD, uid) == cert
