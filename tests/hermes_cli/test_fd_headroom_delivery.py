"""Durable host/profile/role incidents, including ambiguous I/O."""
import json
from unittest.mock import Mock

import pytest
from hermes_cli import fd_headroom_incident as incidents


@pytest.fixture
def delivery(tmp_path):
    cards, creates, comments = {}, [], []
    class Client:
        def create(self, envelope):
            creates.append(envelope)
            key = envelope['id']
            cards.setdefault(key, {'id': f't_{len(cards) + 1:016x}', 'status': 'ready'})
            return cards[key]['id']
        def read(self, tid, board):
            return next((row for row in cards.values() if row['id'] == tid), None)
        def comment(self, envelope):
            comments.append(envelope)
        def close(self):
            pass
    return Client, cards, creates, comments, tmp_path / 'incident.json'


def sample():
    return dict(host='example-host', profile='default', role='gateway', count=800, soft=1000,
                hard=10000, census={'classes': {'resource-leases': 700}}, triggered=True, raised='')


def report(delivery, now, **kwargs):
    cls, _, _, _, path = delivery
    return incidents.report(sample(), now=now, client=cls(), state_path=path, **kwargs)


@pytest.mark.parametrize('closed', ['done', 'archived', 'not found'])
def test_shared_incident_one_card_hourly_and_new_after_closed(delivery, closed):
    _, cards, creates, comments, path = delivery
    first = report(delivery, 10000)
    report(delivery, 10001)
    report(delivery, 13599)
    assert len(creates) == 1 and not comments
    assert creates[0]['id'].startswith('fd-headroom:example-host:default:gateway:')
    report(delivery, 13600)
    report(delivery, 13601)
    assert len(comments) == 1
    if closed == 'not found':
        cards.clear()
    else:
        next(iter(cards.values()))['status'] = closed
    second = report(delivery, 13602)
    assert len(creates) == 2 and creates[0]['id'] != creates[1]['id']
    assert first['incident_started_at'] != second['incident_started_at']
    assert json.loads(path.read_text())['task_id'] == second['task_id']


def test_failed_read_keeps_incident_and_retries_each_tick(delivery):
    cls, _, creates, _, path = delivery
    original = report(delivery, 10000)
    client = cls()
    client.read = Mock(side_effect=TimeoutError('private transport text'))
    for wall in (10300, 10600):
        result = incidents.report(sample(), now=wall, client=client, state_path=path)
    assert len(creates) == 1 and client.read.call_count == 2
    assert result['incident_started_at'] == original['incident_started_at']
    assert result['failing_since'] == 10300
    assert result['error_class'] == 'TimeoutError'


def test_uncertain_create_reuses_frozen_key_without_hourly_retry_delay(delivery):
    cls, cards, creates, _, path = delivery
    class LostAck(cls):
        def create(self, envelope):
            super().create(envelope)
            raise TimeoutError('ack lost')
    for wall in (10000, 10300, 10600):
        incidents.report(sample(), now=wall, client=LostAck(), state_path=path)
    assert len(creates) == 3 and len(cards) == 1
    assert creates[0] == creates[1] == creates[2]
    result = report(delivery, 10900)
    assert result['task_id'] and not result.get('error')


def test_failure_logs_hourly_and_failure_note_after_hour(delivery, caplog):
    cls, _, _, _, path = delivery
    client = cls()
    client.create = Mock(side_effect=OSError('offline'))
    for wall in (10000, 10300, 13600):
        result = incidents.report(sample(), now=wall, client=client, state_path=path)
    assert client.create.call_count == 3
    assert caplog.text.count('card create/report failed') == 2
    assert 'card create failing since' in incidents.failure_note(result, now=13600)


def test_probe_namespace_board_and_separate_state(delivery):
    _, _, creates, _, _ = delivery
    report(delivery, 10000, probe=True, probe_board='fd-probe-isolated')
    envelope = creates[0]
    assert envelope['id'].startswith('fd-headroom-probe:')
    assert envelope['key'] == 'default/fd-probe-isolated/new'
    assert envelope['args']['title'].startswith('[probe]')
    with pytest.raises(ValueError, match='non-default'):
        report(delivery, 11000, probe=True, probe_board='default')


def test_identity_includes_profile_role_and_host(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, 'get_hermes_home', lambda: tmp_path)
    paths = {incidents._path({**sample(), **change}, probe) for change in [
        {}, {'host': 'other'}, {'profile': 'work'}, {'role': 'backend'}] for probe in [False, True]}
    assert len(paths) == 8
