"""Real origin CLI + HTTP/DO + foreign retain + HOME final materialization."""
import json

import pytest

from hermes_cli import kanban_release_executor as executor_reader, kanban_scope_renewal as renewal
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_owner_release_composed as composed

turn = composed.turn
server = composed.server
origin = composed.origin
parties = composed.parties


@pytest.mark.parametrize('stacked', [False, True])
def test_foreign_retainer_real_cli_http_home_same_intent(origin, parties, monkeypatch, stacked):
    home, executor = parties
    stop = composed._home_stop
    capability = composed.ksc.Hub.decision_retain_capability
    monkeypatch.setattr(composed.ksc.Hub, 'decision_retain_capability',
                        lambda self, board: {**capability(self, board), 'owner_release_application': 2})
    def foreign_stop(party, task=None, reason='scope_stopped', refused=False):
        if reason != 'scope_stopped':
            return stop(party, task=task, reason=reason, refused=refused)
        if task is None:
            task = composed.kb.create_task(home.conn, title='foreign retainer', assignee='default')
            pushed = composed.ksc.push(home.conn, home.hub, composed.BOARD, home.name, primary=True)
            assert pushed.rejected == 0
        # Remap local run IDs by occupying HOME IDs, never by changing imported identity.
        with composed.kbc.write_txn(home.conn):
            other = composed.kb.create_task(home.conn, title='ID remap control', assignee='default')
            home.conn.execute("INSERT INTO task_runs(task_id,profile,status,started_at,ended_at) "
                              "VALUES (?,'default','done',1,2)", (other,))
        portable = home.hub.ops(composed.BOARD, [{
            'client_op_id': str(composed.uuid.uuid4()), 'op': 'upsert', 'table': 'tasks', 'uid': task,
            'fields': {'runs_on': '[]'}, 'hlc': composed.stamp(home.name)}])['results']
        assert portable[0]['status'] == 'applied', portable
        executor.resync()
        composed.sync.set_mode(executor.conn, 'primary')
        with monkeypatch.context() as scoped:
            scoped.setattr(composed.kanban_origin, 'gateway_names', lambda: (executor.name, {executor.name}))
            result = stop(executor, task=task, reason=reason, refused=refused)
        home.pull()
        return result
    monkeypatch.setattr(composed, '_home_stop', foreign_stop)
    home, executor, native, task, episode, uid, old = composed.authorize_and_drive_home(
        origin, parties, monkeypatch, stacked=stacked, governed=False)
    retained = home.conn.execute("SELECT uid,run_id FROM task_events WHERE task_id=? AND kind='decision_retained' "
                                 "AND origin_gateway=? ORDER BY id DESC LIMIT 1", (task, executor.name)).fetchone()
    assert retained is not None
    source = executor.conn.execute("SELECT e.run_id,r.uid AS run_uid FROM task_events e JOIN task_runs r ON r.id=e.run_id WHERE e.uid=?", (retained['uid'],)).fetchone()
    assert source['run_id'] != retained['run_id']
    # HOME has no private failure-command for this foreign run, even with earlier HOME history.
    if home.conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_failure_commands'").fetchone():
        assert not home.conn.execute('SELECT 1 FROM kanban_failure_commands WHERE run_uid=?', (source['run_uid'],)).fetchone()
    assert executor.conn.execute('SELECT 1 FROM kanban_failure_commands WHERE run_uid=?', (source['run_uid'],)).fetchone()
    assert retained['uid'] not in {renewal.key(e) for e in renewal.scope_evidence(home.conn, task, episode)}
    certificate = home.hub.owner_release_application(composed.BOARD, uid)
    assert certificate['version'] == 2
    assert [r['episode'] for r in composed.release_receipts(home, task)] == ([old] if old else []) + [episode]
    extra = ('--capability-episode', old) if old else ()
    result = composed.release_cli(native, task, episode, *extra)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)['release_stage'] == 'applied'
    executor.resync()
    assert executor_reader.verified_application(executor.conn, uid) is not None
    assert not decision.execution_restricted(executor.conn, task)
    before = composed.event_inventory(home, task)
    composed.home_tick(home, monkeypatch, rounds=3)
    assert composed.event_inventory(home, task) == before
    assert home.hub.owner_release_application(composed.BOARD, uid) == certificate
