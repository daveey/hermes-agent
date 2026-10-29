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

A held slot records its holder (pid, start time, cwd) in the lock file. When every slot is taken, a waiter
reclaims slots whose holder can never report back: one whose working tree was deleted (killed, then the slot
freed), one whose recorded pid is gone (an inherited fd keeps the lock), and one held longer than
``HERMES_TEST_SLOT_MAX_AGE`` seconds (``tests.slot_max_age_s``; default 45 min, 0 disables). Reclaiming
unlinks the lock file, so the next waiter locks a fresh inode while the old holder keeps the unlinked one:
two orphaned direct ``pytest`` runs whose worktrees had been removed held 2 of hermes-hub's 4 slots for
6h45m and queued every other agent's tests behind them. [daveey]
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import re
import signal
import sys
import time
from collections.abc import Iterator

HELD_ENV = "HERMES_TEST_SLOT_HELD"
MAX_AGE_ENV = "HERMES_TEST_SLOT_MAX_AGE"
DEFAULT_MAX_AGE_S = 45 * 60  # a run_tests.sh slot holds one file (300 s timeout); a direct session rarely needs more
POLL_S = 0.1
REAP_EVERY_S = 5.0


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


def slot_max_age_s() -> int:
    """``HERMES_TEST_SLOT_MAX_AGE`` env, else the host's ``tests.slot_max_age_s``, else 45 min. 0 = no age cap."""
    raw = os.environ.get(MAX_AGE_ENV, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    configured = _config_int("slot_max_age_s")
    return DEFAULT_MAX_AGE_S if configured is None else configured


def _proc_start(pid: int) -> int | None:
    """Linux start time of ``pid`` (clock ticks since boot): tells the recorded holder from a reused pid."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read().decode("ascii", "replace")
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _write_holder(fd: int) -> None:
    """Record who holds this slot, so a waiter can tell a live run from an orphan."""
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    pid = os.getpid()
    record = json.dumps({"pid": pid, "start": _proc_start(pid), "since": time.time(), "cwd": cwd}).encode()
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.pwrite(fd, record, 0)


def _read_holder(fd: int) -> dict | None:
    try:
        record = json.loads(os.pread(fd, 4096, 0) or b"null")
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("pid"), int):
        return None
    return record


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # windows-footgun: ok — host slots are POSIX-only (host_slots_enabled)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cwd_deleted(pid: int, recorded_cwd: str) -> bool:
    """The holder's working directory was removed (its worktree is gone; nobody can read its results)."""
    try:
        live = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        live = None
    if live is not None:
        return live.endswith(" (deleted)")
    return bool(recorded_cwd) and not os.path.isdir(recorded_cwd)


def _stale_reason(record: dict, now: float, max_age: int) -> tuple[str, bool] | None:
    """Why this held slot is an orphan, and whether to kill its holder; None for a live run."""
    pid = record["pid"]
    if pid == os.getpid():
        return None
    start = record.get("start")
    if not _pid_alive(pid) or (isinstance(start, int) and _proc_start(pid) not in (None, start)):
        return f"holder pid {pid} is gone (an inherited fd keeps the lock)", False
    if _cwd_deleted(pid, str(record.get("cwd") or "")):
        # Kill only a holder whose identity is proven (Linux start time); elsewhere just free the slot.
        return f"holder pid {pid} runs in a deleted tree ({record.get('cwd')})", isinstance(start, int)
    since = record.get("since")
    if max_age and isinstance(since, (int, float)) and now - since > max_age:
        return f"holder pid {pid} has held it {int(now - since) // 60} min (cap {max_age // 60} min)", False
    return None


def reclaim_stale_slots(directory: str, count: int, now: float | None = None) -> list[str]:
    """Free every slot in ``directory`` held by an orphan (see the module docstring). Returns one note each.

    Only slots locked right now are judged. A holder in a deleted tree is SIGKILLed (a hung pytest ignored
    SIGTERM); the others are left running. Either way the lock file is unlinked, after checking the path
    still names the inode that was judged, so the slot is free for the next waiter.
    """
    import fcntl

    now = time.time() if now is None else now
    max_age = slot_max_age_s()
    notes: list[str] = []
    for n in range(count):
        path = os.path.join(directory, f"{n}.lock")
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                continue  # free: closing our fd below releases it
            record = _read_holder(fd)
            if record is None:
                continue  # an older runner that records no holder: nothing to judge
            verdict = _stale_reason(record, now, max_age)
            if verdict is None:
                continue
            reason, kill = verdict
            try:
                if os.stat(path).st_ino != os.fstat(fd).st_ino:
                    continue  # already reclaimed and re-taken by another waiter
            except OSError:
                continue
            if kill and record["pid"] > 1:
                with contextlib.suppress(OSError):
                    os.kill(record["pid"], signal.SIGKILL)  # windows-footgun: ok — POSIX-only path
            with contextlib.suppress(OSError):
                os.unlink(path)
            notes.append(f"slot {n}: {reason}" + ("; killed it" if kill else ""))
        finally:
            os.close(fd)
    return notes


def _try_slot(path: str) -> int | None:
    import fcntl

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    _write_holder(fd)
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
    next_reclaim = 0.0
    while True:
        for n in order:
            fd = _try_slot(os.path.join(directory, f"{n}.lock"))
            if fd is not None:
                return fd
        if time.monotonic() >= next_reclaim:
            next_reclaim = time.monotonic() + REAP_EVERY_S
            notes = reclaim_stale_slots(directory, len(order))
            for note in notes:
                sys.stderr.write(f"(host test slots: reclaimed {note})\n")
            if notes:
                sys.stderr.flush()
                continue
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
