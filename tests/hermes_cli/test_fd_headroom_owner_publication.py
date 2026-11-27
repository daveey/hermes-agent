"""FD samples must not lend a pre-reservation contender a live gateway identity."""
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hermes_cli import resource_limits as limits


@pytest.mark.parametrize('overlap', [False, True])
def test_prelock_gateway_sample_does_not_publish_runtime_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlap: bool,
) -> None:
    from gateway import status

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(limits, '_fd_status', {'state': 'ok', 'count': 12})
    sampled = []
    monkeypatch.setattr(limits, 'check_fd_headroom', lambda **kwargs: sampled.append(kwargs))
    monkeypatch.setattr(limits, '_fd_thread', None)
    entered, release = threading.Event(), threading.Event()
    worker = threading.Thread(target=lambda: (entered.set(), release.wait(10)))
    if overlap:
        worker.start()
        assert entered.wait(5)
        monkeypatch.setattr(limits, '_fd_thread', worker)

    def tick() -> None:
        limits.request_fd_headroom(role='gateway')
        if not overlap:
            assert limits._fd_thread is not None
            limits._fd_thread.join(5)
            assert not limits._fd_thread.is_alive()
        assert status.flush_runtime_status(timeout=5)

    try:
        assert not status.owns_gateway_runtime_lock()
        tick()
        assert not (home / 'gateway_state.json').exists()
        if not overlap:
            assert sampled == [{'role': 'gateway'}]  # Sampling still happens before reservation.
        assert status.acquire_gateway_runtime_lock()
        try:
            tick()
            assert status.read_runtime_status()['fd_headroom']['count'] == 12
            # A reservation for a different launch home cannot authorize this writer.
            foreign = tmp_path / 'foreign'
            foreign.mkdir()
            monkeypatch.setenv('HERMES_HOME', str(foreign))
            assert not status.owns_gateway_runtime_lock()
            tick()
            assert not (foreign / 'gateway_state.json').exists()
            monkeypatch.setenv('HERMES_HOME', str(home))
        finally:
            status.release_gateway_runtime_lock()
        before = (home / 'gateway_state.json').read_bytes()
        monkeypatch.setattr(limits, '_fd_status', {'state': 'ok', 'count': 99})
        tick()
        assert (home / 'gateway_state.json').read_bytes() == before
    finally:
        release.set()
        if overlap:
            worker.join(5)
            assert not worker.is_alive()


@pytest.mark.parametrize('overlap', [False, True])
def test_contender_sample_preserves_real_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overlap: bool) -> None:
    from gateway import status

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    assert status.acquire_gateway_runtime_lock()
    try:
        status.publish_runtime_status(gateway_state='running', fd_headroom={'count': 42})
        assert status.flush_runtime_status(timeout=5)
        before = (home / 'gateway_state.json').read_bytes()
        code = """
import threading
from gateway import status
from hermes_cli import resource_limits as limits
assert not status.acquire_gateway_runtime_lock()
limits._fd_status = {'state': 'ok', 'count': 99}
limits.check_fd_headroom = lambda **kwargs: None
release = threading.Event()
worker = threading.Thread(target=release.wait, args=(5,))
if OVERLAP:
    worker.start()
    limits._fd_thread = worker
try:
    limits.request_fd_headroom(role='gateway')
    if not OVERLAP:
        limits._fd_thread.join(5)
        assert not limits._fd_thread.is_alive()
    assert status.flush_runtime_status(timeout=5)
finally:
    release.set()
    if OVERLAP:
        worker.join(5)
""".replace('OVERLAP', repr(overlap))
        child = subprocess.run([sys.executable, '-c', code], env=os.environ.copy(),
                               capture_output=True, text=True, timeout=20, check=False)
        assert child.returncode == 0, child.stdout + child.stderr
        assert (home / 'gateway_state.json').read_bytes() == before
        assert status.read_runtime_status()['pid'] == os.getpid()
    finally:
        status.release_gateway_runtime_lock()
