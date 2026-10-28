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

The cap is ``HERMES_TEST_HOST_SLOTS``, else the host's ``tests.host_slots`` in ``~/.hermes/config.yaml``
(per-host: a laptop the user works on sets a few, so agents' tests never take every core), else
``os.cpu_count()``. A direct ``pytest`` run of this tree (a PM test-environment interpreter rather than
scripts/run_tests.sh) holds one slot for its whole session (tests/conftest.py). [daveey]
"""
from __future__ import annotations

import contextlib
import os
import random
import re
import time
from collections.abc import Iterator

HELD_ENV = "HERMES_TEST_SLOT_HELD"
POLL_S = 0.1


def _config_int(key: str) -> int | None:
    """Per-host ``tests.<key>`` integer from the Hermes config (``HERMES_TEST_SLOT_CONFIG`` overrides the path).

    A laptop the user works on caps the tests every agent runs on it without restarting anything that
    would have to inherit an env var. [daveey]
    """
    path = os.environ.get("HERMES_TEST_SLOT_CONFIG") or os.path.join(os.path.expanduser("~"), ".hermes", "config.yaml")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
    except OSError:
        return None
    try:
        import yaml

        value = ((yaml.safe_load(text) or {}).get("tests") or {}).get(key)
    except Exception:  # no PyYAML in this interpreter, or a config it cannot parse: read the one key
        m = re.search(r"^tests:[ \t]*\n(?:[ \t]+.*\n|[ \t]*\n)*?[ \t]+" + re.escape(key) + r":[ \t]*(\d+)", text, re.MULTILINE)
        value = m.group(1) if m else None
    try:
        return max(0, int(value)) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _config_slots() -> int | None:
    return _config_int("host_slots")


def host_slot_count() -> int:
    """``HERMES_TEST_HOST_SLOTS`` env, else the host's ``tests.host_slots`` config, else ``os.cpu_count()``."""
    raw = os.environ.get("HERMES_TEST_HOST_SLOTS", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    configured = _config_slots()
    if configured is not None:
        return configured
    return os.cpu_count() or 4


def xdist_worker_budget() -> int:
    """Max xdist workers one slot holder may start: ``HERMES_TEST_XDIST_MAX``, else cpu_count // host slots,
    lowered by the host's ``tests.xdist_workers`` config when set.

    A direct ``pytest -n 8`` session holds ONE host slot but runs 8 workers, so four slot holders on a
    16-core Mac ran 32+ workers and the load average passed 100 (ready Kanban cards then sat unclaimed).
    Capping each holder's workers at its share of the cores keeps slots x workers <= cores. [daveey]
    """
    raw = os.environ.get("HERMES_TEST_XDIST_MAX", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    cores = os.cpu_count() or 4
    share = max(1, cores // max(1, host_slot_count()))
    configured = _config_int("xdist_workers")  # per-host: a laptop caps workers below its share, never above
    return max(1, min(share, configured)) if configured is not None else share


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


def acquire_host_slot(on_wait=None) -> int | None:
    """Block until one host slot is held; return its fd (None when the cap does not apply).

    ``on_wait()`` is called once if every slot is taken. Close the fd (``release_host_slot``) to free it;
    a crashed holder frees it with its file descriptors.
    """
    if not host_slots_enabled():
        return None
    directory = host_slot_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    order = list(range(host_slot_count()))
    random.shuffle(order)  # spread runs across slots instead of all racing for slot 0
    waited = False
    while True:
        for n in order:
            fd = _try_slot(os.path.join(directory, f"{n}.lock"))
            if fd is not None:
                return fd
        if not waited and on_wait is not None:
            on_wait()
        waited = True
        time.sleep(POLL_S)


def release_host_slot(fd: int | None) -> None:
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def host_test_slot(on_wait=None) -> Iterator[None]:
    """Hold one host slot for the body. ``on_wait()`` is called once if every slot is taken."""
    fd = acquire_host_slot(on_wait)
    try:
        yield
    finally:
        release_host_slot(fd)
