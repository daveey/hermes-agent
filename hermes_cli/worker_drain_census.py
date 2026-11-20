"""Complete-or-UNKNOWN process census for worker drain.

Counts every process that could be card work: anything owned by the worker
user, plus anything in the user manager's app.slice / hermes.slice, minus an
explicit baseline allowlist of resident infrastructure units and this process.
Unscoped historical work (e.g. an ssh session scope, a detached job outside any
service) is counted, not ignored. If /proc cannot be read completely the answer
is ``None`` (UNKNOWN), never zero. processes.json is never consulted.
"""
from __future__ import annotations

import os
import re
import sys

# Resident infrastructure: cgroup unit leaf names that are not card work.
BASELINE_UNITS = (
    "init.scope", "gpg-agent.service", "dbus.service", "hermes-gateway.service",
    "hermes-gh-conntrace.service", "pipewire.service", "pulseaudio.service",
)
_SCOPED = re.compile(r"/(app|hermes)\.slice(/|$)")


def _read(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _is_baseline(cgroup: str) -> bool:
    return cgroup.rstrip("/").rsplit("/", 1)[-1] in BASELINE_UNITS


def census(proc_root: str = "/proc", uid: int | None = None) -> int | None:
    """Return the count of non-baseline worker processes, or None if UNKNOWN."""
    if not sys.platform.startswith("linux"):
        return None
    uid = os.getuid() if uid is None else uid  # windows-footgun: ok
    me = str(os.getpid())
    try:
        entries = [e for e in os.listdir(proc_root) if e.isdigit()]
    except OSError:
        return None
    if not entries:
        return None
    count = 0
    for pid in entries:
        if pid == me:
            continue
        try:
            if os.stat(f"{proc_root}/{pid}").st_uid != uid:
                cg = _read_cgroup(proc_root, pid)
                if not _SCOPED.search(cg):
                    continue
            else:
                cg = _read_cgroup(proc_root, pid)
            if _is_kernel_thread(proc_root, pid):
                continue
        except FileNotFoundError:
            continue  # exited during the scan: not work
        except OSError:
            return None  # cannot see it: UNKNOWN, not zero
        if not _is_baseline(cg):
            count += 1
    return count


def _read_cgroup(root: str, pid: str) -> str:
    for line in _read(f"{root}/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            return line[3:]
    return ""


def _is_kernel_thread(root: str, pid: str) -> bool:
    return _read(f"{root}/{pid}/cmdline") == "" and "Z (zombie)" not in _read(f"{root}/{pid}/status")


def complete_count() -> int | None:
    """Census gated by the unsent-results check (single exit for zero)."""
    from hermes_cli import machine_persistent_jobs as jobs
    return jobs.census_count(census())
