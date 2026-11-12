"""[daveey] The failed-result-*/ready tempdir is released on every retire path.

Exit 78 (import failure, unusable context) and an exhausted ``StartLimitBurst``
never reach the observer's success cleanup; the tempdir next to the board must
not accumulate. No live service mutation.
"""
import contextlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result_observer as observer
from hermes_cli import kanban_failed_result_service as service
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


def _tempdir_context(context, tmp_path):
    directory = tmp_path / 'failed-result-fixture'
    directory.mkdir()
    ready = directory / 'ready'
    ready.write_text('fixture.service')
    return {**context, 'ready_path': str(ready), 'observer_unit': 'fixture.service',
            'writer_pid': os.getpid(), 'writer_birth': dispatch._process_fingerprint(os.getpid())}


def _import_failure(_):
    raise ModuleNotFoundError("No module named 'ruamel'")


def test_import_failure_retire_releases_tempdir(board, tmp_path, monkeypatch):
    _, context = board
    context = _tempdir_context(context, tmp_path)
    monkeypatch.setattr(observer, 'observe', _import_failure)
    assert observer.main(['observer', json.dumps(context)]) == observer.STARTUP_FAILURE_EXIT
    assert not Path(context['ready_path']).parent.exists()


def test_unusable_context_retire_releases_tempdir(tmp_path):
    context = _tempdir_context({}, tmp_path)  # lacks db/task_id/run_id/claim_lock
    assert observer.main(['observer', json.dumps(context)]) == observer.STARTUP_FAILURE_EXIT
    assert not Path(context['ready_path']).parent.exists()


def test_release_never_masks_retire_exit(board, tmp_path, monkeypatch):
    _, context = board
    context = _tempdir_context(context, tmp_path)
    monkeypatch.setattr(observer, 'observe', _import_failure)
    def refuse(self):
        raise PermissionError('fixture: read-only board directory')
    monkeypatch.setattr(Path, 'rmdir', refuse)
    assert observer.main(['observer', json.dumps(context)]) == observer.STARTUP_FAILURE_EXIT


@pytest.mark.parametrize('ready', ['other/ready', 'failed-result-x/other'])
def test_release_only_touches_failed_result_ready_shape(tmp_path, ready):
    target = tmp_path / ready
    target.parent.mkdir()
    target.write_text('keep')
    context = {'ready_path': str(target)}
    assert observer.main(['observer', json.dumps(context)]) == observer.STARTUP_FAILURE_EXIT
    assert target.read_text() == 'keep'


def test_restarted_observer_recreates_released_tempdir(board, tmp_path, monkeypatch):
    """ExecStopPost removed the directory before this restart; acknowledgment recreates it."""
    _, context = board
    context = _tempdir_context(context, tmp_path)
    ready = Path(context['ready_path'])
    ready.unlink()
    ready.parent.rmdir()
    class EndPoll(Exception):
        pass
    def end_poll(_):
        raise EndPoll
    monkeypatch.setattr(observer.time, 'sleep', end_poll)
    with pytest.raises(EndPoll):
        observer.observe(context)
    assert ready.read_text() == context['observer_unit']


def test_armed_unit_releases_tempdir_after_every_stop(board, monkeypatch):
    """Start-limit-hit is decided by the manager after the last stop: only the unit can clean up."""
    _, context = board
    tools = {'systemd-run': '/usr/bin/systemd-run', 'rm': '/usr/bin/rm', 'rmdir': '/usr/bin/rmdir'}
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=(tools[name],)))
    monkeypatch.setattr(service.time, 'sleep', lambda _: None)
    calls = []
    def submit(command, **kwargs):
        calls.append(command)
        payload = json.loads(command[-1])
        Path(payload['ready_path']).write_text(payload['observer_unit'])
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(service.subprocess, 'run', submit)
    service.arm_service(context, startup_seconds=1)
    options = calls[0][:calls[0].index('--')]
    ready = Path(json.loads(calls[0][-1])['ready_path'])
    assert f'--setenv={service.READY_ENV}={ready}' in options
    assert f'--setenv={service.READY_DIR_ENV}={ready.parent}' in options
    stop_post = [arg.removeprefix('--property=ExecStopPost=') for arg in options
                 if arg.startswith('--property=ExecStopPost=')]
    # '-' prefix: a failed cleanup never changes the unit's result.
    assert stop_post == [f'-/usr/bin/rm -f -- ${{{service.READY_ENV}}}',
                         f'-/usr/bin/rmdir -- ${{{service.READY_DIR_ENV}}}']
    with contextlib.suppress(FileNotFoundError):
        ready.unlink()
        ready.parent.rmdir()


def test_missing_cleanup_tools_keep_previous_unit(monkeypatch):
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=()))
    assert service._ready_cleanup_args(Path('/b/failed-result-x/ready')) == []
