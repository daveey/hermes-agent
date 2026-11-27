"""A healthy sibling cannot rearm another producer's rejected incident."""
import json
import multiprocessing
from collections import deque
from unittest.mock import Mock

import pytest

from hermes_cli import fd_headroom_incident as incidents


def _checker_process(home, connection, probe):
    from hermes_cli import resource_limits as limits
    from hermes_cli import kanban_sync_client as sync
    import hermes_state_guard
    hermes_state_guard._in_test_context = lambda: False
    incidents.get_hermes_home = lambda: home
    sync.load_identity = lambda: None
    limits._resource = Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1)
    limits._resource.getrlimit.return_value = (1000, 10000)
    limits._fd_core_head = lambda: 'a' * 40
    limits._fd_incident = {}
    limits._fd_status.clear()
    limits._fd_samples = deque()
    limits._fd_last_check = None
    calls = []
    posted = set()
    def wire(identity, method, route, body, budget):
        calls.append(method)
        if method == 'GET' and route.rsplit('/', 1)[-1] not in posted:
            raise sync.HubError(404, '{"error":"no such request"}')
        if method == 'POST':
            posted.add(body['id'])
        return {'request': {'state': 'rejected', 'result': {'error': 'fixture refusal'}}}
    sync._bounded_call = wire
    now = 0
    while True:
        count = connection.recv()
        if count is None:
            return
        limits._fd_snapshot = lambda soft: (count, {'classes': {'other': count}})
        kwargs = dict(probe=True, probe_board='fd-proof') if probe else {}
        status = limits.check_fd_headroom({}, role='gateway', now=now, wall_now=10000 + now, **kwargs)
        now += 300
        connection.send((status, list(calls)))


@pytest.mark.parametrize('probe', [False, True])
def test_two_live_checker_processes_do_not_consume_sibling_healthy_transition(tmp_path, probe):
    context = multiprocessing.get_context('fork')
    children = []
    try:
        for _ in range(2):
            parent, child = context.Pipe()
            process = context.Process(target=_checker_process, args=(tmp_path, child, probe))
            process.start()
            child.close()
            children.append((process, parent))
        def check(index, count):
            pipe = children[index][1]
            pipe.send(count)
            assert pipe.poll(15), 'checker child did not answer'
            return pipe.recv()
        for _ in range(6):
            _, leaking_calls = check(0, 600)
            check(1, 100)
        assert leaking_calls.count('POST') == 1, 'healthy sibling must not cause new rejected requests'
        # Only the previously leaking producer's own healthy sample permits rearm.
        check(0, 100)
        check(1, 100)
        _, recovered_calls = check(0, 600)
        assert recovered_calls.count('POST') == 2
        paths = list((tmp_path / 'state' / 'fd_headroom').glob('*.json'))
        assert len(paths) == 1
        assert json.loads(paths[0].read_text())['last_terminal']['receipt']['state'] == 'rejected'
    finally:
        for process, pipe in children:
            if process.is_alive():
                pipe.send(None)
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
            pipe.close()
        assert all(process.exitcode == 0 for process, _ in children)


@pytest.mark.parametrize('probe', [False, True])
def test_cold_discovery_skips_resolved_or_identity_only_history(tmp_path, monkeypatch, probe):
    monkeypatch.setattr(incidents, 'get_hermes_home', lambda: tmp_path)
    from tests.hermes_cli.test_fd_headroom_delivery import sample
    warning = sample()
    path = incidents._path(warning, probe)
    path.parent.mkdir(parents=True)
    for state in ({}, {'error_class': 'ValueError'}, {'task_id': 't_0000000000000001', 'envelope': {'id': 'resolved'}}):
        incidents._save(path, state)
        assert not incidents.has_incident(warning, probe=probe)
    for state in ({'envelope': {'id': 'pending'}}, {'terminal': {'state': 'rejected'}}):
        incidents._save(path, state)
        assert incidents.has_incident(warning, probe=probe)
