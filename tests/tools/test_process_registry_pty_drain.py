"""A PTY session keeps the output its child wrote before the reader thread first ran [daveey].

``PtyProcess.isalive()`` turns False once the child is reaped, so a reader starved of CPU from spawn to
the child's exit used to leave its loop without reading anything and publish an exited session with an
empty output (CI flake of ``TestStdinHelpers.test_close_stdin_allows_eof_driven_process_to_finish``:
``assert 'hello' in ''``). The reader now drains what the master already holds before it records the exit.
"""

from __future__ import annotations

import os
import shlex
import sys

import pytest
from ptyprocess import PtyProcess

from tests.waiting import wait_until
from tools.process_registry import ProcessRegistry
from tools import process_registry_pty_drain as drain_module


class ReadyWaiter:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls = 0
        self.closed = False

    def wait(self, timeout):
        assert timeout == 0
        self.calls += 1
        # Independent finite safety cutoff: a missing production cap must fail, not hang.
        assert self.calls <= 20, "drain did not return at its byte cap"
        return self.ready

    def close(self):
        self.closed = True


class ControlledPty:
    fd = 42

    def __init__(self, chunks=None):
        self.chunks = None if chunks is None else iter(chunks)
        self.requests = []

    def read(self, size):
        self.requests.append(size)
        if self.chunks is None:
            return b"x" * size
        chunk = next(self.chunks)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


def install_waiter(monkeypatch, waiter):
    def open_waiter(fd):
        assert fd == ControlledPty.fd
        return waiter

    monkeypatch.setattr(drain_module, "open_fd_waiter", open_waiter)


@pytest.mark.parametrize("budget", [0, 1, 4095, 4096, 4097, 8193])
def test_always_ready_drain_returns_at_cap(monkeypatch, budget):
    waiter = ReadyWaiter()
    pty = ControlledPty()
    chunks = []
    install_waiter(monkeypatch, waiter)

    drain_module.drain_exited_pty(pty, chunks.append, budget)

    assert sum(map(len, chunks)) == budget
    remaining = budget
    for size in pty.requests:
        assert 0 < size <= min(4096, remaining)
        remaining -= size
    assert remaining == 0
    assert waiter.calls == len(pty.requests)
    assert waiter.closed


@pytest.mark.parametrize("ending", [b"", EOFError(), OSError("closed slave")])
def test_short_output_then_eof(monkeypatch, ending):
    waiter = ReadyWaiter()
    pty = ControlledPty([b"hello", ending])
    chunks = []
    install_waiter(monkeypatch, waiter)

    drain_module.drain_exited_pty(pty, chunks.append, 10)

    assert chunks == [b"hello"]
    assert pty.requests == [10, 5]
    assert waiter.calls == 2
    assert waiter.closed


def test_not_ready_returns_without_reading(monkeypatch):
    waiter = ReadyWaiter(ready=False)
    pty = ControlledPty([])
    chunks = []
    install_waiter(monkeypatch, waiter)

    drain_module.drain_exited_pty(pty, chunks.append, 10)

    assert chunks == []
    assert pty.requests == []
    assert waiter.calls == 1
    assert waiter.closed


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr("hermes_cli.process_identity._update_supervised_pgids", lambda verb, pgids: None)
    return ProcessRegistry()


@pytest.mark.platforms("linux")
def test_reader_that_first_runs_after_child_exit_keeps_its_output(registry, tmp_path, monkeypatch):
    real_isalive = PtyProcess.isalive
    gated = set()

    def isalive_after_exit(self):
        # Hold the reader's first liveness check until the child has exited (WNOWAIT leaves it
        # unreaped for ptyprocess): the schedule of a reader starved from spawn to exit.
        if self.pid not in gated:
            gated.add(self.pid)
            os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)
        return real_isalive(self)

    monkeypatch.setattr(PtyProcess, "isalive", isalive_after_exit)
    command = f"{shlex.quote(sys.executable)} -c \"print('hello')\""
    session = registry.spawn_local(command, cwd=str(tmp_path), use_pty=True)

    poll = wait_until(lambda: (p := registry.poll(session.id))["status"] == "exited" and p,
                      timeout=30, what="PTY session exited")
    assert poll["exit_code"] == 0
    assert "hello" in poll["output_preview"]
