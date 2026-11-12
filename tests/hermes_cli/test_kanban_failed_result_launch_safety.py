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
    monkeypatch.setattr(service.shutil, 'which', lambda name: '/usr/bin/systemd-run')
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
