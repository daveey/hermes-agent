"""Non-signalling process probes and canonical observer launch paths."""
import json
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import _startup_fast
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result as reconcile
from hermes_cli import kanban_failed_result_service as service
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


@pytest.mark.parametrize('exists', [True, False, PermissionError(), OSError()])
def test_process_probe_never_signals(monkeypatch, exists):
    import os

    def signal_forbidden(*args):
        pytest.fail('death proof must never signal a process')

    def probe(pid):
        assert pid == 123
        if isinstance(exists, Exception):
            raise exists
        return exists

    monkeypatch.setattr(os, 'kill', signal_forbidden)
    monkeypatch.setattr(psutil, 'pid_exists', probe)
    monkeypatch.setattr(dispatch, '_process_fingerprint', lambda pid: None)
    assert reconcile.process_gone(123, 'epoch|1') is (exists is False)


@pytest.mark.parametrize('bom', [False, True])
def test_service_uses_canonical_install_root_and_reads_readiness(board, tmp_path, monkeypatch, bom):
    _, context = board
    root = tmp_path / 'canonical-install'
    root.mkdir()
    monkeypatch.setattr(_startup_fast, 'project_root_str', lambda: str(root))
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=('/usr/bin/systemd-run',)))
    calls = []

    def submit(command, **kwargs):
        calls.append(command)
        payload = json.loads(command[-1])
        Path(payload['ready_path']).write_text(payload['observer_unit'],
                                              encoding='utf-8-sig' if bom else 'utf-8')
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(service.subprocess, 'run', submit)
    unit = service.arm_service(context, startup_seconds=.1)
    assert unit.endswith('.service')
    assert f'--working-directory={root}' in calls[0]


def test_service_execstart_interpreter_imports_runtime_dependencies(board, monkeypatch):
    """The unit runs with ONLY its --setenv environment: its interpreter must import ruamel/hermes_yaml.

    A managed gateway boots the PM store python (no third-party packages) with PYTHONPATH popped and
    its venv on sys.path in-process, so ``sys.executable -m`` without a pinned PYTHONPATH crash-looped
    on ``No module named 'ruamel'`` (2336 tracebacks/2h on hermes-hub).
    """
    import os
    import subprocess
    import sys

    import ruamel.yaml
    from cron import scheduler_worker_env

    base = Path(sys.base_prefix) / 'bin' / f'python{sys.version_info.major}.{sys.version_info.minor}'
    if not base.is_file():
        pytest.skip('no base interpreter beside this venv')
    bare = subprocess.run([str(base), '-I', '-c', 'import ruamel.yaml'], capture_output=True, env={}, check=False)
    if bare.returncode == 0:
        pytest.skip('base interpreter imports ruamel by itself: not a store-python layout')
    real_run = subprocess.run  # service.subprocess IS subprocess: keep the real one for the probe
    _, context = board
    site_packages = Path(ruamel.yaml.__file__).resolve().parents[2]
    monkeypatch.delenv('PYTHONPATH', raising=False)
    monkeypatch.setattr(sys, 'executable', str(base))
    monkeypatch.setattr(scheduler_worker_env, '_committed_dependency_site_packages', lambda root: site_packages)
    monkeypatch.setattr(service, 'locate_command', lambda name: SimpleNamespace(command=('/usr/bin/systemd-run',)))
    calls = []

    def submit(command, **kwargs):
        calls.append(command)
        payload = json.loads(command[-1])
        Path(payload['ready_path']).write_text(payload['observer_unit'], encoding='utf-8')
        return SimpleNamespace(returncode=0, stderr='')

    monkeypatch.setattr(service.subprocess, 'run', submit)
    service.arm_service(context, startup_seconds=.1)
    command = calls[0]
    split = command.index('--')
    options, exec_start = command[:split], command[split + 1:]
    env = dict(o.removeprefix('--setenv=').split('=', 1) for o in options if o.startswith('--setenv='))
    cwd = next(o.removeprefix('--working-directory=') for o in options if o.startswith('--working-directory='))
    module = exec_start.index('-m')
    assert exec_start[module + 1] == 'hermes_cli.kanban_failed_result_observer'
    probe = [*exec_start[:module], '-c', 'import ruamel.yaml, hermes_yaml, hermes_cli.kanban_failed_result_observer']
    done = real_run(probe, cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert os.path.realpath(exec_start[0]) == os.path.realpath(base)
