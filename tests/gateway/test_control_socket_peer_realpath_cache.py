"""The control socket's per-connection peer check must not realpath the home on the event loop
for every connection (t_01a1d3ac14d0b207): under FS pressure each one stalled session-ticket
bootstraps for seconds."""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("linux")  # SO_PEERCRED


class _Writer:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, _key):
        return self._sock


def _slow(real):
    def wrapper(*args, **kwargs):
        threading.Event().wait(0.2)  # health: allow HX013 -- a slow realpath IS the behaviour under test
        return real(*args, **kwargs)
    return wrapper


def test_repeat_peer_check_reuses_the_home_realpath(tmp_path, monkeypatch):
    """Test B: after a warm call, a repeat peer check costs no realpath."""
    from gateway.control_socket import GatewayControlServer
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    ours, _theirs = socket.socketpair(socket.AF_UNIX)
    server = GatewayControlServer(home)
    assert server._posix_peer_subject(_Writer(ours)) == f"uid:{os.getuid()}"
    # Path.resolve only: the test harness's home-IO guard itself calls os.path.realpath per syscall.
    monkeypatch.setattr(Path, "resolve", _slow(Path.resolve))
    started = time.monotonic()
    assert server._posix_peer_subject(_Writer(ours)) == f"uid:{os.getuid()}"
    assert time.monotonic() - started < 0.05


def test_home_swapped_for_a_symlink_is_refused_at_once(tmp_path):
    """The home itself is still lstat'ed per connection: the cached verdict never admits a home
    that became a symlink."""
    from gateway.control_socket import GatewayControlServer
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    ours, _theirs = socket.socketpair(socket.AF_UNIX)
    server = GatewayControlServer(home)
    assert server._posix_peer_subject(_Writer(ours)) == f"uid:{os.getuid()}"
    home.rename(tmp_path / "moved")
    home.symlink_to(tmp_path / "moved")
    assert server._posix_peer_subject(_Writer(ours)) is None


def test_symlinked_ancestor_refusal_is_not_cached(tmp_path, monkeypatch):
    from gateway.control_socket import GatewayControlServer
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    ours, _theirs = socket.socketpair(socket.AF_UNIX)
    server = GatewayControlServer(home)
    verdicts = iter([False, True])
    monkeypatch.setattr(Path, "absolute", lambda self: self if next(verdicts) else self / "x")
    assert server._posix_peer_subject(_Writer(ours)) is None
    assert server._posix_peer_subject(_Writer(ours)) == f"uid:{os.getuid()}"
