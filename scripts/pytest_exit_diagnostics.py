"""Capture late pytest shutdown stacks for the derived-session exit-hang investigation.

Loaded explicitly before capture by run_tests_launch, only for the affected file.
The descriptor and C watchdog intentionally survive pytest teardown and atexit:
closing them there would hide exactly the interpreter-finalization hang we need.
This observes shutdown; it does not change its deadline, cleanup, or exit status.
"""
import atexit
import faulthandler
import os

import pytest

_FD = os.dup(2)  # Before pytest redirects stderr to a capture temporary file.
os.set_inheritable(_FD, False)


def _mark(phase, write=os.write, fd=_FD, pid=os.getpid()):
    write(fd, f"[pytest-exit pid={pid}] {phase}\n".encode())


def pytest_addoption(parser):
    parser.addini("exit_diagnostics_delay", "Seconds before a late-exit stack dump", default="20")


def _arm(config):
    faulthandler.dump_traceback_later(
        float(config.getini("exit_diagnostics_delay")), repeat=True, file=_FD,
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    _mark("sessionfinish")
    _arm(session.config)
    # This runs before import-time atexit handlers (LIFO), not after all cleanup.
    atexit.register(_mark, "atexit-enter")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_unconfigure(config):
    _mark("unconfigure-enter")
    try:
        yield
    finally:
        # Other plugins may cancel the process-wide watchdog during teardown.
        _arm(config)
        _mark("unconfigure-return")
