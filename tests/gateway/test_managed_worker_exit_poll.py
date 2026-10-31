"""The background-exit observer tolerates reaping during its status read."""
import subprocess
import sys

import psutil
import pytest

from tests.gateway.test_managed_worker_launch import _background_exited
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
def test_background_exit_poll_reaped_during_status(monkeypatch):
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as child:
        try:
            assert not _background_exited(child.pid)
            original = psutil.Process.status
            observed = []

            def reap_before_status(process):
                if process.pid == child.pid:
                    child.kill()
                    child.wait(timeout=30)
                    observed.append(process.pid)
                return original(process)

            monkeypatch.setattr(psutil.Process, 'status', reap_before_status)
            assert _background_exited(child.pid)
            assert observed == [child.pid]
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=30)


@pytest.mark.platforms('linux')
def test_background_exit_poll_zombie_missing_and_access_denied(monkeypatch):
    with subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                          stdin=subprocess.PIPE) as child:
        try:
            assert not _background_exited(child.pid)
            with monkeypatch.context() as patch:
                def denied(process):
                    raise psutil.AccessDenied(process.pid)
                patch.setattr(psutil.Process, 'status', denied)
                with pytest.raises(psutil.AccessDenied):
                    _background_exited(child.pid)
            child.kill()
            wait_until(lambda: psutil.Process(child.pid).status() == psutil.STATUS_ZOMBIE,
                       timeout=30, what='child zombie before reap')
            assert _background_exited(child.pid)
            child.wait(timeout=30)
            assert _background_exited(child.pid)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=30)
