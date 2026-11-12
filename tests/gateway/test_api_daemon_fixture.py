"""``api_daemon`` re-boots only for the free-port race; other api_server fatals fail at once."""
from contextlib import contextmanager
import json

import pytest

from tests.gateway.fixtures import local_recovery_probe as probe


def fake_boots(monkeypatch, home, records):
    """Stand in for ``daemon``: boot N publishes ``records[N]`` as its api_server record in
    ``gateway_state.json`` and writes a restart.log naming the boot."""
    boots = []

    @contextmanager
    def daemon(root, home_arg, env, *, barrier):
        assert not barrier
        pid = 1000 + len(boots)
        boots.append(env['API_SERVER_PORT'])
        (home / 'restart.log').write_text(f'BOOT_LOG_{len(boots)}\n')
        record = records[len(boots) - 1]
        (home / 'gateway_state.json').write_text(json.dumps({'pid': pid, 'platforms': {'api_server': record}}))
        yield object(), {'pid': pid}

    ports = iter(range(41000, 41100))
    monkeypatch.setattr(probe, 'daemon', daemon)
    monkeypatch.setattr(probe, 'free_port', lambda: next(ports))
    return boots


def test_non_port_fatal_fails_after_one_boot_with_its_code(tmp_path, monkeypatch):
    boots = fake_boots(monkeypatch, tmp_path, [{'state': 'fatal', 'error_code': 'api_server_key_invalid'}] * 5)
    with pytest.raises(AssertionError) as raised:
        with probe.api_daemon(tmp_path, tmp_path, {}, lambda port: None):
            pytest.fail('a fatal api_server must not yield')
    assert len(boots) == 1
    assert 'api_server_key_invalid' in str(raised.value) and 'BOOT_LOG_1' in str(raised.value)
    assert 'never owned a free port' not in str(raised.value)


def test_port_in_use_reboots_on_a_fresh_port(tmp_path, monkeypatch):
    lost = {'state': 'fatal', 'error_code': 'api_server_port_in_use'}
    boots = fake_boots(monkeypatch, tmp_path, [lost, lost, {'state': 'connected'}])
    written = []
    with probe.api_daemon(tmp_path, tmp_path, {}, written.append) as (_, desc, port):
        assert desc == {'pid': 1002} and port == 41002
    assert boots == ['41000', '41001', '41002'] and written == [41000, 41001, 41002]


def test_port_in_use_every_boot_reports_the_race(tmp_path, monkeypatch):
    boots = fake_boots(monkeypatch, tmp_path, [{'state': 'fatal', 'error_code': 'api_server_port_in_use'}] * 3)
    with pytest.raises(AssertionError, match='never owned a free port in 3 boots'):
        with probe.api_daemon(tmp_path, tmp_path, {}, lambda port: None, attempts=3):
            pass
    assert len(boots) == 3


def test_listener_state_timeout_carries_restart_log(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'RPC_DEADLINE', 0.1)
    (tmp_path / 'restart.log').write_text('DAEMON_SAID_WHY\n')
    (tmp_path / 'gateway_state.json').write_text(json.dumps({'pid': 7, 'platforms': {'api_server': {'state': 'connecting'}}}))
    with pytest.raises(AssertionError, match='DAEMON_SAID_WHY'):
        probe.api_listener_state(tmp_path, {'pid': 7})
