"""Real Board evidence vocabulary and late/error monitoring regressions."""
from unittest.mock import Mock

import pytest

from hermes_cli import fd_headroom_incident as incidents
from hermes_cli import kanban_sync_client as sync
from hermes_cli import resource_limits as limits
from hermes_cli.fd_headroom_transport import Client


@pytest.mark.parametrize('state', ['never_existed', 'unknown', 'deleted', 'deleted_row'])
@pytest.mark.parametrize('triggered', [True, False])
def test_real_board_evidence_through_delivery(monkeypatch, tmp_path, state, triggered):
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    creates, reads = [], []
    tid = 't_0123456789abcdef'
    evidence = dict(state='deleted' if state == 'deleted_row' else state,
                    table='tasks', uid=tid, head=4)
    if state == 'deleted_row':
        evidence['row'] = dict(id=tid, status='ready', deleted_at=10001)
    def request(*args):
        reads.append(args)
        return evidence
    monkeypatch.setattr(sync, '_bounded_call', request)
    class DeliveryClient(Client):
        def create(self, envelope):
            creates.append(envelope)
            return tid
    path = tmp_path / 'incident.json'
    sample = dict(host='example', profile='default', role='gateway', triggered=True,
                  count=800, soft=1000, hard=10000, census={}, raised='')
    first = incidents.report(sample, now=10000, client=DeliveryClient(), state_path=path)
    sample['triggered'] = triggered
    for tick in range(24):
        current = incidents.report(sample, now=10300 + tick * 300,
                                   client=DeliveryClient(), state_path=path)
        if state.startswith('deleted') and triggered:
            break
    if state in {'never_existed', 'unknown'}:
        assert len(creates) == 1 and len(reads) == 24
        assert current['task_id'] == first['task_id']
        assert current['incident_started_at'] == first['incident_started_at']
        assert current['error_class'] == 'PendingVisibility'
        assert current['card_unreplicated_since'] == current['failing_since'] == 10300
    elif triggered:
        assert len(creates) == 2 and creates[0]['id'] != creates[1]['id']
        assert not current.get('error')
    else:
        assert len(creates) == len(reads) == 1
        assert not current.get('task_id') and not current.get('envelope')
        assert not current.get('error')


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_interval', 300)
    monkeypatch.setattr(limits, '_fd_pid', limits.os.getpid())
    monkeypatch.setattr(limits, '_fd_status', {})
    monkeypatch.setattr(limits, '_fd_incident', {})
    monkeypatch.setattr(limits, '_fd_check_warning', None)
    limits._fd_samples.clear()
    resource = Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1)
    resource.getrlimit.return_value = (10000, 10000)
    snapshot = Mock(return_value=(100, {}))
    monkeypatch.setattr(limits, '_resource', resource)
    monkeypatch.setattr(limits, '_fd_snapshot', snapshot)
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    monkeypatch.setattr(limits, '_notify_fd_incident', Mock(return_value={}))
    return resource, snapshot


def test_late_ticks_at_maximum_interval_keep_mature_slope(guard):
    from hermes_cli.fd_headroom_config import validated
    _, snapshot = guard
    maximum = max(i for i in range(30, 602) if not validated(
        {'runtime': {'fd_headroom': {'interval_seconds': i}}})[1])
    settings = {'runtime': {'fd_headroom': {'interval_seconds': maximum}}}
    for tick in range(8):
        snapshot.return_value = (100 + tick * 400, {})
        sample = limits.check_fd_headroom(settings, now=tick * (maximum + 1), wall_now=tick * 600)
        assert sample is not None
        if tick >= 3:
            assert sample['slope'] > 0 and sample['triggered']
    assert all(t >= 7 * (maximum + 1) - 1800 for t, _ in limits._fd_samples)


@pytest.mark.parametrize('failure', ['snapshot', 'limit', 'config'])
def test_failed_check_status_identifies_config_boundary(guard, monkeypatch, caplog, failure):
    from hermes_cli import config
    resource, snapshot = guard
    if failure == 'config':
        monkeypatch.setattr(config, 'load_config_readonly', Mock(side_effect=OSError('private config')))
    elif failure == 'limit':
        resource.getrlimit.side_effect = OSError('private resource')
    else:
        snapshot.side_effect = OSError('private snapshot')
    for now in [0, 300]:
        limits.check_fd_headroom(None if failure == 'config' else {}, now=now)
    status = limits.fd_headroom_status()
    assert status['state'] == ('config_error' if failure == 'config' else 'capped')
    assert status['error_class'] == 'OSError'
    assert 'private' not in str(status)
    assert caplog.text.count('fd-headroom: check failed') == 1
