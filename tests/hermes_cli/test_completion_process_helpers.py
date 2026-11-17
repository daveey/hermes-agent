"""Real process disappearance between a custody check and status/signal."""

import subprocess
import sys

import psutil
import pytest

from tests.hermes_cli.completion_process_helpers import descendant_exited, reap_descendant


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("operation", ["status", "kill"])
def test_owned_descendant_disappears_at_checked_seam(monkeypatch, operation):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    proc = psutil.Process(child.pid)
    original = getattr(proc, operation)
    reached = []

    def disappear():
        # The helper has already observed this exact process alive. Remove the
        # real OS process before the subsequent psutil read/signal, not a fake
        # NoSuchProcess exception or a scheduler-dependent sleep.
        assert child.poll() is None
        child.kill()
        child.wait(timeout=5)
        assert not psutil.pid_exists(child.pid)
        reached.append(operation)
        return original()

    monkeypatch.setattr(proc, operation, disappear)
    try:
        if operation == "status":
            assert descendant_exited(proc)
        else:
            reap_descendant(proc)
        assert reached == [operation]
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("operation", ["status", "kill"])
def test_owned_descendant_does_not_swallow_access_denied(monkeypatch, operation):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    proc = psutil.Process(child.pid)

    def refused():
        raise psutil.AccessDenied(proc.pid)

    monkeypatch.setattr(proc, operation, refused)
    try:
        if operation == "kill":
            assert not descendant_exited(proc)
        else:
            assert proc.is_running()
        with pytest.raises(psutil.AccessDenied):
            (descendant_exited if operation == "status" else reap_descendant)(proc)
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_retained_identity_never_signals_replacement(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    proc = psutil.Process(child.pid)
    try:
        # Simulate an old retained identity pointing at our live child's PID.
        # No foreign process is involved and psutil's real reuse guard runs.
        monkeypatch.setattr(proc, "_ident", (proc.pid, proc.create_time() - 1))
        assert descendant_exited(proc)
        reap_descendant(proc)
        assert child.poll() is None, "cleanup signalled the replacement identity"
    finally:
        child.kill()
        child.wait(timeout=5)


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_owned_descendant_live_and_cleanup():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    proc = psutil.Process(child.pid)
    try:
        assert not descendant_exited(proc)
        reap_descendant(proc)
        child.wait(timeout=5)
        assert descendant_exited(proc)
        reap_descendant(proc)  # already gone is idempotent
    finally:
        child.kill()
        child.wait(timeout=5)
