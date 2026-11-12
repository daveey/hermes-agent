"""[daveey] A retiring observer stops its own unit, so units without RestartPreventExitStatus retire too.

Transient units keep the properties they were submitted with. Units armed by a
gateway older than the exit-78 fix carry ``Restart=on-failure`` and nothing else,
so each retire was restarted forever (52k restarts, ~1 core, ~290k journal
lines/h on worker-4). A queued stop job cancels the manager's auto-restart. No
live service mutation: ``systemctl`` and ``/proc/self/cgroup`` are fixtures.
"""
import json
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_failed_result_observer as observer

UNIT = 'hermes-failed-result-174699-3952684799857.service'
CONTEXT = {'db': '/b/kanban.db', 'task_id': 't_x', 'run_id': 454, 'claim_lock': 'host:1',
           'observer_unit': UNIT}


def _import_failure(_):
    raise ModuleNotFoundError("No module named 'ruamel'")


@pytest.fixture
def systemctl(monkeypatch):
    calls = []
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/systemctl' if name == 'systemctl' else None)
    def run(command, **kwargs):
        calls.append(command)
        return observer.subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(observer.subprocess, 'run', run)
    monkeypatch.setattr(observer, 'observe', _import_failure)
    return calls


def _cgroup(monkeypatch, path):
    real = Path.read_text
    def read_text(self, *args, **kwargs):
        if str(self) == '/proc/self/cgroup':
            return f'0::{path}\n'
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read_text)


def test_import_failure_queues_stop_of_own_unit(systemctl, monkeypatch):
    _cgroup(monkeypatch, f'/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}')
    assert observer.main(['observer', json.dumps(CONTEXT)]) == observer.STARTUP_FAILURE_EXIT
    assert systemctl == [['/usr/bin/systemctl', '--user', '--no-block', 'stop', UNIT]]


def test_unusable_context_queues_stop_of_own_unit(systemctl, monkeypatch):
    _cgroup(monkeypatch, f'/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}')
    context = {'observer_unit': UNIT}  # lacks db/task_id/run_id/claim_lock
    assert observer.main(['observer', json.dumps(context)]) == observer.STARTUP_FAILURE_EXIT
    assert systemctl == [['/usr/bin/systemctl', '--user', '--no-block', 'stop', UNIT]]


@pytest.mark.parametrize('cgroup', [
    '/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-failed-result-1-2.service',
    '/user.slice/user-1000.slice/session-3.scope',
])
def test_never_stops_a_unit_it_does_not_run_in(systemctl, monkeypatch, cgroup):
    _cgroup(monkeypatch, cgroup)
    assert observer.main(['observer', json.dumps(CONTEXT)]) == observer.STARTUP_FAILURE_EXIT
    assert systemctl == []


@pytest.mark.parametrize('unit', ['gateway.service', 'hermes-failed-result-1-2.scope', 7, None])
def test_only_failed_result_service_names_are_stopped(systemctl, monkeypatch, unit):
    _cgroup(monkeypatch, f'/user.slice/user-1000.slice/user@1000.service/app.slice/{unit}')
    assert observer.main(['observer', json.dumps({**CONTEXT, 'observer_unit': unit})]) == observer.STARTUP_FAILURE_EXIT
    assert systemctl == []


def test_stop_failure_never_masks_retire_exit(systemctl, monkeypatch):
    _cgroup(monkeypatch, f'/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}')
    def refuse(command, **kwargs):
        raise observer.subprocess.TimeoutExpired(command, 5)
    monkeypatch.setattr(observer.subprocess, 'run', refuse)
    assert observer.main(['observer', json.dumps(CONTEXT)]) == observer.STARTUP_FAILURE_EXIT


def test_missing_systemctl_or_cgroup_is_a_no_op(systemctl, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda name: None)
    _cgroup(monkeypatch, f'/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}')
    assert observer.main(['observer', json.dumps(CONTEXT)]) == observer.STARTUP_FAILURE_EXIT
    real = Path.read_text
    def no_proc(self, *args, **kwargs):
        if str(self) == '/proc/self/cgroup':
            raise FileNotFoundError(self)
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', no_proc)
    monkeypatch.setattr(shutil, 'which', lambda name: '/usr/bin/systemctl')
    assert observer.main(['observer', json.dumps(CONTEXT)]) == observer.STARTUP_FAILURE_EXIT
    assert systemctl == []
