"""Exercise CLI parser/dispatch against a real temporary control socket."""
import argparse
import asyncio
import json
import threading
import time

import pytest

from gateway.control_socket import GatewayControlServer, query_gateway_control
from hermes_cli.subcommands.gateway import build_gateway_parser
from tests.waiting import async_wait_until


def _parser():
    from hermes_cli.gateway import gateway_command
    parser = argparse.ArgumentParser()
    build_gateway_parser(parser.add_subparsers(dest='command'), cmd_gateway=gateway_command,
                         cmd_proxy=lambda args: None, cmd_gateway_enroll=lambda args: None)
    return parser


def test_cli_and_control_defaults_agree():
    from gateway.control_profile import validate_params
    args = _parser().parse_args(['gateway', 'profile'])
    assert (args.seconds, args.hz, args.top) == validate_params({})
    assert args.hz == 25


@pytest.mark.platforms('posix')
@pytest.mark.asyncio
async def test_cli_profile_and_socket_remain_responsive(tmp_path, monkeypatch, capsys):
    from hermes_cli.gateway import gateway_command
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    args = _parser().parse_args(['gateway', 'profile', '--seconds', '1', '--hz', '100', '--json'])
    server = GatewayControlServer(tmp_path, verb_handlers={'identify': lambda: {'alive': True},
                                                         'status': lambda: {'ready': True}})
    assert await server.start()
    try:
        task = asyncio.create_task(asyncio.to_thread(gateway_command, args))
        await async_wait_until(lambda: any(t.name == 'gateway-profiler' for t in threading.enumerate()), timeout=3)
        for verb, expected in [('identify', {'alive': True}), ('status', {'ready': True})]:
            start = time.monotonic()
            answer = await asyncio.to_thread(query_gateway_control, tmp_path, verb)
            assert answer == expected
            assert time.monotonic() - start < 0.5
        await asyncio.wait_for(task, 5)
        result = json.loads(capsys.readouterr().out)
        assert result['seconds'] == 1 and result['hz'] == 100 and result['samples'] > 0
        assert result['threads']
        args = _parser().parse_args(['gateway', 'profile', '--seconds', '1', '--hz', '10'])
        await asyncio.to_thread(gateway_command, args)
        output = capsys.readouterr().out
        assert 'CPU' in output and 'SELF' in output and 'INCLUSIVE' in output
    finally:
        await server.stop()


def test_cli_profile_timeout_exceeds_duration(monkeypatch, tmp_path, capsys):
    from hermes_cli.gateway import gateway_command
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    calls = []
    def query(home, verb, *, params, timeout):
        calls.append((home, verb, params, timeout))
        return dict(seconds=60, hz=200, samples=0, threads=[])
    monkeypatch.setattr('gateway.control_socket.query_gateway_control', query)
    gateway_command(_parser().parse_args(['gateway', 'profile', '--seconds', '60', '--hz', '200', '--json']))
    home, verb, params, timeout = calls[0]
    assert home == tmp_path and verb == 'profile'
    assert params == {'seconds': 60, 'hz': 200, 'top': 40}
    assert timeout > 60
    assert json.loads(capsys.readouterr().out)['seconds'] == 60
