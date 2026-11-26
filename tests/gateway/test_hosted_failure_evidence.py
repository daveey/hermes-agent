"""Controlled failed settlement versus running-task deadline diagnostic signatures."""
import json
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as state, hosted_rooms
from tests.gateway.fixtures import hosted_failure_evidence as evidence
from tui_gateway.hosted_room_driver import HostedRoomRuntime


@pytest.mark.parametrize('failed', [True, False], ids=['failed-settlement', 'running-starvation'])
def test_failure_capture_preserves_the_settlement_signature(tmp_path, failed):
    home, target = tmp_path / 'host', tmp_path / 'member'
    home.mkdir()
    target.mkdir()
    db = home / 'state.db'
    clock = lambda: 100.0
    hosted_rooms.create_room(db, room_id='unknown', name='fixture',
                             members=[{'profile': 'default', 'handle': 'one'}],
                             authority_gateway_id='gateway', now=90)
    identity = state.TaskIdentity(room_id='unknown', task_id='follower', thread_id='thread', turn_id='turn')
    state.admit_task(db, identity, payload={'target_profile': 'default', 'prompt': 'DO_NOT_DUMP_PROMPT',
                                          'source_event_seq': 1}, clock=clock)
    lease = state.acquire_lease(db, room_id='unknown', gateway_id='gateway', authority_epoch=1,
                                process_generation='process', ttl_seconds=30, clock=clock)
    attempt = state.start_task(db, identity, lease, expected_cancel_generation=0, clock=clock)
    if failed:
        driver = SimpleNamespace(db_path=db, clock=clock, _record_task_error=lambda *_: None)
        HostedRoomRuntime._settle_failure_if_current(
            driver, attempt, RuntimeError('resolve_exact failed Authorization: Bearer fixture-secret-value'))
    (target / 'restart.log').write_text('x' * 9000 + '\nmember transport diagnostic\n')
    snapshot = {'counts': {'cancelled': 1, 'settled': 1, 'failed' if failed else 'running': 1},
                'last_error': 'Authorization: Bearer snapshot-secret-value'}
    with pytest.raises(AssertionError, match='the follower never settled after discard: ') as caught:
        evidence.assert_follower_deadline(False, home, target, snapshot, 2)
    report = json.loads(str(caught.value).split(': ', 1)[1])
    task, = report['tasks']
    assert task['status'] == ('failed' if failed else 'running')
    assert task['settlement_status'] == ('failed' if failed else None)
    assert task['terminal_at'] == (100.0 if failed else None)
    assert ('resolve_exact failed' in (task['error'] or '')) is failed
    assert report['request_count'] == 2
    assert report['snapshot']['counts'] == snapshot['counts']
    assert 'member transport diagnostic' in report['logs']['member/restart.log']
    assert len(report['logs']['member/restart.log']) <= evidence.TAIL_BYTES
    assert len(str(caught.value)) < 24500
    assert all(secret not in str(caught.value) for secret in (
        'DO_NOT_DUMP_PROMPT', 'fixture-secret-value', 'snapshot-secret-value'))


def test_passing_poll_does_not_collect_evidence(monkeypatch, tmp_path):
    def forbidden(*args):
        pytest.fail('passing path collected diagnostics')
    monkeypatch.setattr(evidence, 'follower_failure_evidence', forbidden)
    evidence.assert_follower_deadline(True, tmp_path, tmp_path, {}, 0)


def test_missing_db_still_reports_original_failure_without_creating_it(tmp_path):
    with pytest.raises(AssertionError, match='the follower never settled after discard: ') as caught:
        evidence.assert_follower_deadline(False, tmp_path, tmp_path, {'counts': {'running': 1}}, 2)
    assert 'OperationalError' in str(caught.value)
    assert not (tmp_path / 'state.db').exists()
