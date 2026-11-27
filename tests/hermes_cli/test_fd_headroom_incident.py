"""Real process-shared flock state and prolonged failure status integration."""
import logging
import multiprocessing
from unittest.mock import Mock

from hermes_cli import fd_headroom_incident as incidents, resource_limits as limits


def _process_report(path, marker, start):
    class Client:
        def create(self, envelope):
            with marker.open('a') as out:
                out.write(envelope['id'] + '\n')
            return 't_0123456789abcdef'
        def read(self, tid, board):
            return {'id': tid, 'status': 'ready'}
        def close(self):
            pass
    assert start.wait(3)
    incidents.report(dict(host='example-host', profile='default', role='gateway', count=500,
                         soft=1000, census={}, triggered=True), now=10000,
                     client=Client(), state_path=path)


def test_two_processes_share_one_card(tmp_path):
    ctx = multiprocessing.get_context('fork')
    start = ctx.Event()
    marker, path = tmp_path / 'creates', tmp_path / 'state.json'
    children = [ctx.Process(target=_process_report, args=(path, marker, start)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        start.set()
        for child in children:
            child.join(5)
            assert child.exitcode == 0
        assert len(marker.read_text().splitlines()) == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(3)


def test_failed_create_warning_and_public_process_status(monkeypatch, caplog, tmp_path):
    wall = [10000.0]
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_incident', {})
    monkeypatch.setattr(limits, '_fd_status', {})
    limits._fd_samples.clear()
    monkeypatch.setattr(limits, '_resource', Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1,
                        getrlimit=Mock(return_value=(100, 1000))))
    monkeypatch.setattr(limits, '_fd_snapshot', lambda _: (50, {}))
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    attempts = []
    class Client:
        def create(self, envelope):
            attempts.append(envelope)
            raise OSError('/private/offline')
        def close(self):
            pass
    monkeypatch.setattr(limits, '_notify_fd_incident', lambda s, **kw:
                        incidents.report(s, now=wall[0], client=Client(), state_path=tmp_path / 'state.json'))
    with caplog.at_level(logging.WARNING):
        for now in (0, 300, 3600):
            wall[0] = 10000 + now
            limits.check_fd_headroom({}, now=now, wall_now=wall[0])
    assert len(attempts) == 3
    assert 'card create failing since' in caplog.text
    state = limits.fd_headroom_status()
    assert state['count'] == 50 and state['soft'] == 100
    assert state['create_state'] == 'failing' and state['failing_since'] == 10000
    assert state['error_class'] == 'OSError'
    assert 'private' not in str(state)
