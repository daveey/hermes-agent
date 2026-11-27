"""Durable coalescing; external courier replaced, state/locking remain real."""
import json
import logging
from unittest.mock import Mock

import pytest
from hermes_cli import fd_headroom_incident as incidents, resource_limits as limits


@pytest.fixture
def courier(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, '_path', lambda probe: tmp_path / ('probe.json' if probe else 'state.json'))
    monkeypatch.setattr(incidents, '_pending_task', lambda _: (None, 'pending'))
    rows, creates, comments = [], [], []
    def run(argv):
        if argv[1] == 'tasks':
            return json.dumps(rows)
        if argv[1] == 'create-task':
            creates.append(argv)
            key = argv[argv.index('--id') + 1]
            rows.append(dict(id=f't_{len(creates):016x}', idempotency_key=key, status='ready'))
        else:
            comments.append(argv)
        return '{}'
    return run, rows, creates, comments


def sample():
    return dict(host='example-host', pid=123, role='gateway', count=80, soft=100,
                hard=1000, census={'resource-leases': 70}, slope=0.1, core='a' * 40,
                raised='fd-headroom: raised soft 100→400')


def report(courier, now, **kwargs):
    return incidents.report(sample(), now=now, run=courier[0], cli='courier', **kwargs)


@pytest.mark.parametrize('closed', ['done', 'archived'])
def test_one_card_per_incident_and_hourly_comments(courier, closed):
    _, rows, creates, comments = courier
    report(courier, 0)
    report(courier, 1)
    report(courier, 3599)
    assert len(creates) == 1 and len(comments) == 0
    report(courier, 3600)
    report(courier, 3601)
    assert len(creates) == 1 and len(comments) == 1
    assert 'raised soft' in comments[0][comments[0].index('--arg') + 1]
    rows[0]['status'] = closed
    report(courier, 3602)
    assert len(creates) == 2
    assert creates[0][2] == 'hub' and creates[0][creates[0].index('--board') + 1] == 'default'
    assert 'runs_on=["pool:worker"]' in creates[0]


def test_probe_archive_then_immediate_second_process_is_suppressed(courier):
    _, rows, creates, _ = courier
    report(courier, 0, probe=True)
    assert creates[0][3].startswith('[probe]')
    rows[0]['status'] = 'archived'
    report(courier, 1, probe=True)
    assert len(creates) == 1


def test_uncertain_create_replays_same_frozen_envelope_hourly(courier):
    run, _, creates, _ = courier
    def lost_ack(argv):
        if argv[1] == 'create-task':
            creates.append(argv)
            raise OSError('lost acknowledgement')
        return run(argv)
    incidents.report(sample(), now=0, run=lost_ack, cli='courier')
    incidents.report(sample(), now=1, run=lost_ack, cli='courier')
    incidents.report({**sample(), 'count': 90}, now=3600, run=lost_ack, cli='courier')
    assert len(creates) == 2 and creates[0] == creates[1]


def test_raise_queued_during_comment_cooldown_is_not_lost(courier):
    run, _, creates, comments = courier
    incidents.report({**sample(), 'raised': ''}, now=0, run=run, cli='courier')
    report(courier, 10)
    assert not comments
    incidents.report({**sample(), 'raised': '', 'triggered': False}, now=3600, run=run, cli='courier')
    assert len(creates) == 1 and len(comments) == 1
    assert 'raised soft' in comments[0][comments[0].index('--arg') + 1]


def test_failed_create_backoff_warning_and_process_status(courier, monkeypatch, caplog):
    run, _, _, _ = courier
    attempts = []
    def failing(argv):
        if argv[1] == 'create-task':
            attempts.append(argv)
            raise OSError('offline')
        return run(argv)
    wall = [10000.0]
    monkeypatch.setattr(incidents.time, 'time', lambda: wall[0])
    monkeypatch.setattr(limits, '_fd_last_check', None)
    limits._fd_samples.clear()
    limits._fd_status.clear()
    monkeypatch.setattr(limits, '_resource', Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1,
                        getrlimit=Mock(return_value=(100, 1000))))
    monkeypatch.setattr(limits, '_fd_snapshot', lambda _: (50, {'other': 50}))
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    monkeypatch.setattr(limits, '_notify_fd_incident', lambda s, **kw:
                        incidents.report(s, run=failing, cli='courier'))
    with caplog.at_level(logging.WARNING):
        limits.check_fd_headroom({}, now=0)
        wall[0] += 300
        limits.check_fd_headroom({}, now=300)
        assert len(attempts) == 1
        wall[0] += 3300
        limits.check_fd_headroom({}, now=3600)
    assert len(attempts) == 2
    assert 'card create failing since' in caplog.text
    state = limits.fd_headroom_status()
    assert state['count'] == 50 and state['soft'] == 100
    assert state['incident']['failure_since'] == 10000
    assert 'offline' in state['incident']['error']
    assert 'card create failing since' in state['card_error']
