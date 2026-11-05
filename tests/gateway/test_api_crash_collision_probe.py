"""Controlled reproduction: the dashboard takes the released API port on restart."""
import subprocess
from pathlib import Path
import re

from tests.gateway.test_api_crash_recovery import test_api_crash_queue_and_identified_result_retry as exercise


def test_dashboard_takes_released_port(tmp_path, monkeypatch):
    original = subprocess.Popen

    restarts = 0

    def spawn(command, **kwargs):
        nonlocal restarts
        if command[1:] == ['-m', 'gateway.run']:
            restarts += 1
        if command[1:] == ['-m', 'gateway.run'] and restarts == 2:
            log = Path(kwargs['env']['HERMES_HOME']) / 'logs/gateway.log'
            ports = re.findall(r'API server listening on http://127\.0\.0\.1:([0-9]+)', log.read_text(encoding='utf-8'))
            previous_port = int(ports[-1])
            code = (
                'import functools, runpy; from gateway import run_api; '
                'run_api.start_gateway_api = functools.partial(run_api.start_gateway_api, '
                f'port={previous_port}); '
                'runpy.run_module("gateway.run", run_name="__main__")'
            )
            command = [command[0], '-c', code]
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', spawn)
    exercise(tmp_path)
