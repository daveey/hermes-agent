"""Crash recovery must not reuse a predecessor's now-occupied API port."""
import os
from pathlib import Path
import re
import socket
import subprocess

import pytest

from tests.gateway.test_api_crash_recovery import test_api_crash_queue_and_identified_result_retry as exercise


@pytest.mark.skipif(os.name == 'nt', reason='socket custody uses POSIX pass_fds; ordinary recovery runs everywhere')
@pytest.mark.parametrize('collision_restart', [1, 2])
def test_api_recovers_while_previous_port_is_owned(tmp_path, monkeypatch, collision_restart):
    original = subprocess.Popen
    boots = 0
    # Never reclaim a released port: another test (or the successor's API) may
    # already own it. The predecessor serves its real aiohttp app on this socket;
    # our duplicate stays open across SIGKILL, making the collision deterministic.
    with socket.socket() as custody:
        custody.bind(('127.0.0.1', 0))
        custody.listen()
        occupied_port = custody.getsockname()[1]

        def spawn(command, **kwargs):
            nonlocal boots
            initial = command[1:] == [str(Path(__file__).parent / 'fixtures/api_recovery_daemon.py')]
            restart = command[1:] == ['-m', 'gateway.run']
            if initial or restart:
                boot = boots
                boots += 1
                if boot == collision_restart - 1:
                    wrapper = str(Path(__file__).parent / 'fixtures/api_collision_daemon.py')
                    command = [command[0], wrapper, *([command[1]] if initial else [])]
                    kwargs['env'] = {**kwargs['env'], 'HERMES_TEST_API_SOCKET': str(custody.fileno())}
                    kwargs['pass_fds'] = (custody.fileno(),)
            return original(command, **kwargs)

        monkeypatch.setattr(subprocess, 'Popen', spawn)
        exercise(tmp_path)
        ports = [int(port) for port in re.findall(
            r'API server listening on http://127\.0\.0\.1:([0-9]+)',
            (tmp_path / 'state/logs/gateway.log').read_text(encoding='utf-8'))]
        assert boots == 3 and len(ports) == 3, (boots, ports)
        assert ports[collision_restart - 1] == occupied_port, ports
        assert all(port != occupied_port for port in ports[collision_restart:]), ports
