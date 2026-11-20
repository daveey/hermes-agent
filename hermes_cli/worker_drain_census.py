"""Positive lower-bound worker census; incomplete observations never emit zero.

A unit name exempts neither a daemon nor any descendants. A complete idle proof
needs host cgroup/namespace traversal and exact manifest/live adapter identities;
that part is not implemented yet. Until then this producer returns positive work
or UNKNOWN. It cannot enable scale-in even when drain activation is present.
processes.json is not an authority.
"""
from __future__ import annotations

import os
import re
import sys

_SCOPED = re.compile(r'/(app|hermes)\.slice(/|$)')
_PF_KTHREAD = 0x200000


def _read(path: str) -> str:
    with open(path, 'rb') as fh:
        return fh.read().decode('utf-8', 'replace')


def census(proc_root: str = '/proc', uid: int | None = None) -> int | None:
    """Count observed worker userspace processes, never assert complete idle.

    Foreign-UID/root and unstable/missing observations do not prove absence of
    work. A positive number remains a conservative lower bound if the inventory
    is incomplete. No guessed infrastructure exemptions or implicit zero.
    """
    if not sys.platform.startswith('linux'):
        return None
    uid = os.getuid() if uid is None else uid  # windows-footgun: ok
    me = str(os.getpid())
    try:
        entries = [e for e in os.listdir(proc_root) if e.isdigit()]
    except OSError:
        return None
    count = 0
    for pid in entries:
        if pid == me:
            continue
        try:
            cg = _read_cgroup(proc_root, pid)
            if not cg:
                continue  # unsupported membership: not an absence proof
            if os.stat(f'{proc_root}/{pid}').st_uid == uid or _SCOPED.search(cg):
                if not _is_kernel_thread(proc_root, pid):
                    count += 1
        except (OSError, ValueError, IndexError):
            continue  # incomplete entry: positive lower bound or UNKNOWN below
    return count or None


def _read_cgroup(root: str, pid: str) -> str:
    for line in _read(f'{root}/{pid}/cgroup').splitlines():
        if line.startswith('0::'):
            return line[3:]
    return ''


def _is_kernel_thread(root: str, pid: str) -> bool:
    # Empty cmdline alone also describes exec races and ordinary userspace that
    # changed argv. PF_KTHREAD is field 9; comm may contain spaces/parentheses.
    fields = _read(f'{root}/{pid}/stat').rsplit(')', 1)[1].split()
    return bool(int(fields[6]) & _PF_KTHREAD) and _read(f'{root}/{pid}/cmdline') == ''


def complete_count() -> int | None:
    """Positive/UNKNOWN observation through the unsent-results gate."""
    from hermes_cli import machine_persistent_jobs as jobs
    return jobs.census_count(census())
