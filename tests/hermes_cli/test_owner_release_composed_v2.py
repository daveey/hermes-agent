"""Real CLI/socket/DO/HOME/public executor v2, without any signer call.

The two advertisement READs are fixture overlays only; production remains off.
"""
import json

import pytest

from hermes_cli import kanban_release_executor as executor_reader, kanban_spend_decision as decision
from tests.hermes_cli import test_owner_release_composed as composed

turn = composed.turn
server = composed.server
origin = composed.origin
parties = composed.parties


@pytest.mark.parametrize('stacked', [False, True])
def test_ordinary_no_signer_real_transport_and_public_executor(origin, parties, monkeypatch, stacked):
    capability = composed.ksc.Hub.decision_retain_capability
    monkeypatch.setattr(composed.ksc.Hub, 'decision_retain_capability',
                        lambda self, board: {**capability(self, board), 'owner_release_application': 2})
    def no_signer(*args):
        pytest.fail('ordinary path called the signed-window producer')
    monkeypatch.setattr(composed, 'install_allowance', no_signer)
    home, executor, native, task, episode, uid, old = composed.authorize_and_drive_home(
        origin, parties, monkeypatch, stacked=stacked, governed=False)
    certificate = home.hub.owner_release_application(composed.BOARD, uid)
    assert certificate['version'] == 2
    assert certificate['governance'] == {'state': 'ungoverned'}
    assert 'allowance' not in certificate
    proposal = certificate['operation']['proposal']
    assert proposal['version'] == 2
    assert proposal['accounting']['semantics'] == 'core_home_ordinary_v1'
    payload = json.loads(next(e['payload'] for e in proposal['events'] if e['kind'] == 'spend_budget_set'))
    assert payload['runs'] == proposal['accounting']['spent_runs'] + certificate['consumption']['intent']['runs']
    assert [r['episode'] for r in composed.release_receipts(home, task)] == ([old] if old else []) + [episode]
    extra = ('--capability-episode', old) if old else ()
    result = composed.release_cli(native, task, episode, *extra)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout)['release_stage'] == 'applied'
    executor.resync()
    public = executor_reader.verified_application(executor.conn, uid)
    assert public is not None
    evidence = executor.hub.row_evidence(composed.BOARD, 'task_events', public['result_event']['uid'])['owner_release_applied_v1']
    assert evidence['version'] == 1
    assert 'governance' not in evidence and 'accounting' not in evidence and 'allowance' not in evidence
    assert not decision.execution_restricted(executor.conn, task)
    before = composed.event_inventory(home, task)
    composed.home_tick(home, monkeypatch, rounds=3)
    assert composed.event_inventory(home, task) == before
    assert home.hub.owner_release_application(composed.BOARD, uid) == certificate
