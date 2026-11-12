"""A launchd launcher parked at the updater's ancestor fence is not a live gateway."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from gateway import control_socket
from hermes_cli import update_cmd_posix_pause as pause
from hermes_cli import update_gateway_fence as fence
from hermes_cli import update_pause_record as record


@pytest.fixture
def parked(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pause, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    monkeypatch.setattr(pause, '_drain_budget', lambda: 30.0)
    monkeypatch.setattr(pause, '_alive', lambda *a: True)
    ask = Mock(return_value=None)
    sent = Mock()
    disarm = Mock()
    monkeypatch.setattr(pause, '_ask_to_drain', ask)
    monkeypatch.setattr(record, 'mark_stop_sent', sent)
    monkeypatch.setattr(pause, '_disarm', disarm)
    owner = Mock(return_value=77)
    monkeypatch.setattr(fence, 'ancestor_fence_owner', owner)
    query = Mock(return_value=None)
    monkeypatch.setattr(control_socket, 'query_gateway_control', query)
    launcher = Mock()
    launcher.cmdline.return_value = ['/usr/bin/python3', str(tmp_path / 'bin/hermes-gateway-launcher'), '--home', str(tmp_path), '--', '/fixture/hermes', 'gateway', 'run']
    launcher.children.return_value = []
    launcher.is_running.return_value = True
    monkeypatch.setattr(psutil, 'Process', Mock(return_value=launcher))
    entry = dict(pid=4242, kind='launchd', label='ai.hermes.gateway', home=str(tmp_path))
    return SimpleNamespace(entry=entry, now=now, ask=ask, sent=sent, disarm=disarm,
                           owner=owner, query=query, launcher=launcher)


def test_parked_launcher_returns_without_pause_wait_or_disarm(parked):
    pause._stop_gateways({}, [parked.entry])
    assert parked.now == [0.0]
    parked.ask.assert_not_called()
    parked.sent.assert_not_called()
    parked.disarm.assert_not_called()
    parked.owner.assert_called_with([Path(parked.entry['home'])])


def test_live_answering_gateway_still_waits(parked):
    parked.query.return_value = {'pid': 5000}
    parked.ask.return_value = True
    with pytest.raises(pause.PauseRefused, match='still draining after 30s'):
        pause._stop_gateways({}, [parked.entry])
    assert parked.now[0] >= 30
    parked.ask.assert_called_once()
    parked.sent.assert_called_once()
    parked.disarm.assert_not_called()


@pytest.mark.parametrize('case', ['foreign-fence', 'wrong-home', 'has-child', 'unknown-argv', 'dead', 'systemd'])
def test_only_positively_parked_launcher_is_exempt(parked, case):
    setups = {
        'foreign-fence': lambda: setattr(parked.owner, 'return_value', None),
        'wrong-home': lambda: parked.launcher.cmdline.return_value.__setitem__(3, '/another/home'),
        'has-child': lambda: setattr(parked.launcher.children, 'return_value', [Mock()]),
        'unknown-argv': lambda: setattr(parked.launcher.cmdline, 'side_effect', psutil.AccessDenied(4242)),
        'dead': lambda: setattr(parked.launcher.is_running, 'return_value', False),
        'systemd': lambda: parked.entry.__setitem__('kind', 'systemd'),
    }
    setups[case]()
    with pytest.raises(pause.PauseRefused):
        pause._stop_gateways({}, [parked.entry])
    parked.ask.assert_called_once()


def test_signed_wrapper_with_one_parked_launcher(parked, monkeypatch):
    wrapper = Mock()
    from hermes_cli.gateway_launchd import _SIGNED_WRAPPER_SOURCE
    wrapper.cmdline.return_value = [str(Path.home() / 'Applications/Hermes Python.app/Contents/MacOS/python3'),
                                    '-I', '-c', _SIGNED_WRAPPER_SOURCE, 'exec launcher']
    wrapper.children.return_value = [parked.launcher]
    wrapper.is_running.return_value = True
    monkeypatch.setattr(psutil, 'Process', Mock(return_value=wrapper))
    pause._stop_gateways({}, [parked.entry])
    parked.ask.assert_not_called()


@pytest.mark.parametrize('case', ['unknown-wrapper', 'wrapped-child', 'wrapped-socket', 'multiple',
                                      'birth', 'owner-changed', 'children-error', 'wrong-launcher'])
def test_ambiguous_wrapper_and_identity_fail_closed(parked, monkeypatch, case):
    from gateway import status
    from hermes_cli.gateway_launchd import _SIGNED_WRAPPER_SOURCE
    wrapper = Mock()
    wrapper.cmdline.return_value = [str(Path.home() / 'Applications/Hermes Python.app/Contents/MacOS/python3'),
                                    '-I', '-c', _SIGNED_WRAPPER_SOURCE, 'exec launcher']
    wrapper.children.return_value = [parked.launcher]
    wrapper.is_running.return_value = True
    monkeypatch.setattr(psutil, 'Process', Mock(return_value=wrapper))
    def wrong_birth():
        parked.entry['ct'] = 123
        monkeypatch.setattr(status, 'get_process_start_time', lambda pid: 456)

    setups = {
        'unknown-wrapper': lambda: wrapper.cmdline.return_value.__setitem__(3, 'unrelated source'),
        'wrapped-child': lambda: setattr(parked.launcher.children, 'return_value', [Mock()]),
        'wrapped-socket': lambda: setattr(parked.query, 'return_value', {}),
        'multiple': lambda: wrapper.children.return_value.append(Mock()),
        'birth': wrong_birth,
        'owner-changed': lambda: setattr(parked.owner, 'side_effect', [77, 78]),
        'children-error': lambda: setattr(parked.launcher.children, 'side_effect', psutil.AccessDenied(4242)),
        'wrong-launcher': lambda: parked.launcher.cmdline.return_value.__setitem__(1, '/untrusted/hermes-gateway-launcher'),
    }
    setups[case]()
    with pytest.raises(pause.PauseRefused):
        pause._stop_gateways({}, [parked.entry])
    parked.ask.assert_called_once()


def test_recorded_nonexempt_entry_is_not_reclassified_after_custody(parked, monkeypatch):
    check = Mock(return_value=True)
    monkeypatch.setattr(fence, 'parked_launchd_launcher', check)
    with pytest.raises(pause.PauseRefused):
        pause._stop_gateways({}, [parked.entry], fence_checked=True)
    check.assert_not_called()
    parked.ask.assert_called_once()
    parked.sent.assert_called_once()


@pytest.mark.parametrize('mixed', [False, True])
def test_pause_discovery_leaves_restart_custody_with_fence(parked, monkeypatch, mixed):
    from gateway import status
    ordinary = dict(pid=4343, kind='systemd', unit='fixture.service', home=parked.entry['home'])
    monkeypatch.setattr(pause, '_discover_systemd', lambda: [ordinary] if mixed else [])
    monkeypatch.setattr(status, 'get_process_start_time', lambda pid: 1)
    monkeypatch.setattr(record, 'identity', lambda pid: {'ct': 'ct:1'})
    monkeypatch.setattr(record, 'mark_stop_requested', Mock())
    monkeypatch.setattr(record, 'sync', Mock())
    stop = Mock()
    monkeypatch.setattr(pause, '_stop_gateways', stop)
    monkeypatch.setattr(pause, '_discover_launchd', lambda: [parked.entry])
    monkeypatch.setattr(pause, '_left_running', lambda u, j: (u, j, []))
    monkeypatch.setattr(pause, '_discover_bare', lambda p: ([], []))
    monkeypatch.setattr(pause, '_deferral_streak', lambda: None)
    write = Mock()
    monkeypatch.setattr(record, 'record_pause', write)
    token = pause._empty_token()
    assert pause._pause(token) is None
    assert token['resume_needed'] is mixed
    assert token['posix_units'] == ([ordinary] if mixed else [])
    if mixed:
        write.assert_called_once()
        stop.assert_called_once_with(
            token, [ordinary], parked.now[0] + pause._PRE_SWAP_DRAIN_WAIT_S, fence_checked=True)
    else:
        write.assert_not_called()
        stop.assert_not_called()


def test_missing_home_fails_closed(parked):
    parked.entry.pop('home')
    assert not fence.parked_launchd_launcher(parked.entry)
    parked.entry['home'] = ''
    assert not fence.parked_launchd_launcher(parked.entry)


@pytest.mark.parametrize('case', ['home-flag', 'separator', 'no-command', 'offset-two'])
def test_launcher_exact_argv(parked, case):
    argv = parked.launcher.cmdline.return_value
    mutations = {
        'home-flag': lambda: argv.__setitem__(2, '--other'),
        'separator': lambda: argv.__setitem__(4, 'other'),
        'no-command': lambda: argv.__delitem__(slice(5, None)),
        'offset-two': lambda: argv.insert(1, '-X'),
    }
    mutations[case]()
    assert not fence.parked_launchd_launcher(parked.entry)


@pytest.mark.parametrize('case', ['dead-root', 'dead-child', 'extra-arg', 'wrong-dir', 'wrong-name', 'no-exec'])
def test_wrapper_exact_identity(parked, monkeypatch, case):
    from hermes_cli.gateway_launchd import _SIGNED_WRAPPER_SOURCE
    wrapper = Mock()
    argv = [str(Path.home() / 'Applications/Hermes Python.app/Contents/MacOS/python3'),
            '-I', '-c', _SIGNED_WRAPPER_SOURCE, 'exec launcher']
    wrapper.cmdline.return_value = argv
    wrapper.children.return_value = [parked.launcher]
    wrapper.is_running.return_value = True
    mutations = {
        'dead-root': lambda: setattr(wrapper.is_running, 'return_value', False),
        'dead-child': lambda: setattr(parked.launcher.is_running, 'return_value', False),
        'extra-arg': lambda: argv.append('extra'),
        'wrong-dir': lambda: argv.__setitem__(0, '/untrusted/python3'),
        'wrong-name': lambda: argv.__setitem__(0, str(Path(argv[0]).parent / 'bash')),
        'no-exec': lambda: argv.__setitem__(4, 'launcher'),
    }
    mutations[case]()
    monkeypatch.setattr(psutil, 'Process', Mock(return_value=wrapper))
    assert not fence.parked_launchd_launcher(parked.entry)


def test_missing_psutil_fails_closed(parked, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == 'psutil':
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', missing)
    assert not fence.parked_launchd_launcher(parked.entry)
