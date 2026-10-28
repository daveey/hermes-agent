"""Host-wide cap on concurrent per-file pytest processes, shared by every run on the machine.

Each ``run_tests_parallel`` run sizes its pool to ``os.cpu_count()`` without knowing about the others.
Several agents testing at once (one run per worktree) multiplied that: four runs started 64 pytest
processes on a 16-core Mac, the load average passed 200, and the gateway's event loop missed its
Discord heartbeat. A file subprocess now runs only while its thread holds one of
``HERMES_TEST_HOST_SLOTS`` (default ``os.cpu_count()``) flock slots in ``HERMES_TEST_SLOT_DIR``
(default ``/tmp/hermes-test-slots-<uid>``). A slot is a held ``LOCK_EX`` on ``<dir>/<n>.lock``, so a
crashed run frees its slots with its file descriptors.

A run started from inside a slot holder (a test that runs the runner) inherits
``HERMES_TEST_SLOT_HELD=1`` and skips the cap, so nested runs cannot wait on slots their own parent
holds. ``HERMES_TEST_HOST_SLOTS=0`` disables the cap. Non-POSIX hosts are uncapped.
"""
from __future__ import annotations

import contextlib
import os
import random
import time
from collections.abc import Iterator

HELD_ENV = "HERMES_TEST_SLOT_HELD"
POLL_S = 0.1


def host_slot_count() -> int:
    raw = os.environ.get("HERMES_TEST_HOST_SLOTS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return os.cpu_count() or 4


def host_slot_dir() -> str:
    return os.environ.get("HERMES_TEST_SLOT_DIR") or f"/tmp/hermes-test-slots-{os.getuid()}"  # windows-footgun: ok — POSIX test host slots  # no-tmp: ok — host-wide shared slot dir must be outside per-run scratch


def host_slots_enabled() -> bool:
    return os.name == "posix" and host_slot_count() > 0 and os.environ.get(HELD_ENV) != "1"


def _try_slot(path: str) -> int | None:
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


@contextlib.contextmanager
def host_test_slot(on_wait=None) -> Iterator[None]:
    """Hold one host slot for the body. ``on_wait()`` is called once if every slot is taken."""
    if not host_slots_enabled():
        yield
        return
    directory = host_slot_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    order = list(range(host_slot_count()))
    random.shuffle(order)  # spread runs across slots instead of all racing for slot 0
    waited = False
    while True:
        for n in order:
            fd = _try_slot(os.path.join(directory, f"{n}.lock"))
            if fd is None:
                continue
            try:
                yield
            finally:
                os.close(fd)
            return
        if not waited and on_wait is not None:
            on_wait()
        waited = True
        time.sleep(POLL_S)
