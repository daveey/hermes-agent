"""Birth-qualified rearm remains safe after fork, PID reuse and bounded eviction."""
import multiprocessing
from unittest.mock import Mock

import pytest

from hermes_cli import fd_headroom_incident as incidents
from tests.hermes_cli.test_fd_headroom_delivery import sample


def _send_birth(connection):
    try:
        connection.send(incidents._producer())
    finally:
        connection.close()


@pytest.mark.platforms('posix')
def test_minted_parent_birth_is_renewed_after_fork(monkeypatch):
    monkeypatch.setattr(incidents, '_producer_token', None)
    parent_birth = incidents._producer()
    context = multiprocessing.get_context('fork')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_send_birth, args=(child,))
    process.start()
    child.close()
    try:
        assert parent.poll(15)
        child_birth = parent.recv()
        assert child_birth != parent_birth
        assert child_birth.split(':')[0] == str(process.pid)
        assert child_birth.split(':')[1] != parent_birth.split(':')[1]
    finally:
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent.close()
    assert process.exitcode == 0


def test_same_pid_new_birth_cannot_consume_old_rearm(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, '_producer_token', None)
    old_birth = incidents._producer()
    path = tmp_path / 'incident.json'
    state = {}
    incidents._new_envelope(sample(), state, 10000, False, 'default')
    state.update(terminal={'state': 'rejected', 'result': {'error': 'refused'}},
                 terminal_rearmed_by=[old_birth])
    incidents._save(path, state)
    monkeypatch.setattr(incidents, '_producer_token', None)  # Exec/restart with reused PID.
    assert incidents._producer() != old_birth
    client = Mock()
    current = incidents.report(sample(), state_path=path, client=client, now=10300)
    assert current['envelope'] == state['envelope']
    assert current['terminal'] == state['terminal']
    client.create.assert_not_called()


def test_newest_64_births_can_rearm_and_evicted_birth_cannot(tmp_path, monkeypatch):
    path = tmp_path / 'incident.json'
    state = {}
    incidents._new_envelope(sample(), state, 10000, False, 'default')
    state['terminal'] = {'state': 'rejected', 'result': {'error': 'refused'}}
    incidents._save(path, state)
    birth = ''
    monkeypatch.setattr(incidents, '_producer', lambda: birth)
    client = Mock()
    healthy = {**sample(), 'triggered': False}
    for index in range(70):
        birth = f'{index}:birth'
        current = incidents.report(healthy, state_path=path, client=client, now=10300 + index)
    assert current['terminal_rearmed_by'] == [f'{index}:birth' for index in range(6, 70)]
    birth = '0:birth'
    current = incidents.report(sample(), state_path=path, client=client, now=11000)
    assert current['envelope'] == state['envelope']
    client.create.assert_not_called()
    birth = '69:birth'
    client.create.return_value = 't_0000000000000002'
    current = incidents.report(sample(), state_path=path, client=client, now=11300)
    client.create.assert_called_once()
    assert current['last_terminal']['envelope'] == state['envelope']
    assert current['task_id'] == 't_0000000000000002'
