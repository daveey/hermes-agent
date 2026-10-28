"""Notice holder liveness uses the portable probe and preserves PID-reuse protection."""

import os
import subprocess
import sys

import pytest

from gateway import status
from hermes_cli import kanban_db_notify as notify


def test_holder_liveness_does_not_signal_processes(monkeypatch):
    def unexpected_signal(*args):
        raise AssertionError("the holder must delegate to the portable probe")

    monkeypatch.setattr(os, "kill", unexpected_signal)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    assert notify._holder_alive({"inflight_pid": os.getpid(), "inflight_start": None})


def test_holder_liveness_with_real_alive_and_reaped_process():
    assert notify._holder_alive({"inflight_pid": os.getpid(), "inflight_start": None})
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert not notify._holder_alive({"inflight_pid": child.pid, "inflight_start": None})


@pytest.mark.parametrize("pid", [None, "invalid", 0, -1, object(), 10**100])
def test_invalid_holder_is_not_alive(pid):
    assert not notify._holder_alive({"inflight_pid": pid, "inflight_start": None})


@pytest.mark.parametrize(
    ("exists", "recorded", "current", "expected"),
    [
        (False, None, None, False),
        (True, None, None, True),
        (True, 10000, None, True),
        (True, 10000, 10000, True),
        (True, 10000, 10000 + status.START_TIME_DRIFT_TOLERANCE, True),
        (True, 10000, 10001 + status.START_TIME_DRIFT_TOLERANCE, False),
    ],
)
def test_holder_uses_portable_liveness_and_start_fingerprint(
    monkeypatch, exists, recorded, current, expected,
):
    monkeypatch.setattr(status, "_pid_exists", lambda pid: exists)
    monkeypatch.setattr(notify, "_process_start", lambda pid: current)
    assert notify._holder_alive({"inflight_pid": os.getpid(), "inflight_start": recorded}) is expected


@pytest.mark.parametrize(
    ("error", "expected"), [(PermissionError, True), (ProcessLookupError, False), (OSError, False)],
)
def test_holder_probe_errors_preserve_conservative_ownership(monkeypatch, error, expected):
    def probe(pid):
        raise error

    monkeypatch.setattr(status, "_pid_exists", probe)
    assert notify._holder_alive({"inflight_pid": os.getpid(), "inflight_start": None}) is expected
