"""Positive lower-bound worker census; incomplete observations never emit zero.

A unit name exempts neither a daemon nor any descendants. A complete idle proof
needs host cgroup/namespace traversal and exact manifest/live adapter identities;
that part is not implemented yet. Until then this producer returns positive work
or UNKNOWN. It cannot enable scale-in even when drain activation is present.
processes.json is not an authority.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass

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


# ---------------------------------------------------------------------------
# Manifest census: zero is provable only when EVERY manifest adapter proves zero.
# ---------------------------------------------------------------------------
BASELINE_PATH = '/var/lib/hermes-worker/drain-baseline.json'
ADAPTERS = ('host_processes', 'work_leases', 'local_results')  # the complete manifest
ZERO, POSITIVE, UNKNOWN = 'zero', 'positive', 'unknown'


@dataclass(frozen=True)
class Observation:
    state: str
    count: int = 0
    reason: str = ''


def load_baseline(path: str = BASELINE_PATH) -> frozenset | None:
    """Exact idle identities (uid, cgroup, exe) captured at install. None = no usable baseline.

    Trusted only when root-owned, a regular non-symlink file and not group/world writable. A unit
    name or comm never matches; descendants are judged separately by ``observe_processes``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            return None
        doc = json.loads(os.read(fd, 1 << 20))
        if doc.get('version') != 1 or not isinstance(doc.get('idle'), list):
            return None
        return frozenset((int(e['uid']), str(e['cgroup']), str(e['exe'])) for e in doc['idle'])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    finally:
        os.close(fd)


def observe_processes(proc_root: str = '/proc', baseline: frozenset | None = None) -> Observation:
    """Every userspace process is work unless it exactly matches a live baseline identity and so
    do all of its descendants. Any unreadable entry or missing baseline cannot prove zero."""
    if not sys.platform.startswith('linux'):
        return Observation(UNKNOWN, reason='not linux')
    try:
        pids = [e for e in os.listdir(proc_root) if e.isdigit()]
    except OSError:
        return Observation(UNKNOWN, reason='proc unreadable')
    me = str(os.getpid())
    procs: dict = {}  # pid -> (ppid, identity)
    unreadable = 0
    for pid in pids:
        if pid == me:
            continue
        try:
            if _is_kernel_thread(proc_root, pid):
                continue
            ppid = _read(f'{proc_root}/{pid}/stat').rsplit(')', 1)[1].split()[1]
            ident = (os.stat(f'{proc_root}/{pid}').st_uid, _read_cgroup(proc_root, pid),
                     os.readlink(f'{proc_root}/{pid}/exe'))
            procs[pid] = (ppid, ident)
        except (OSError, ValueError, IndexError):
            if os.path.exists(f'{proc_root}/{pid}'):
                unreadable += 1  # alive but unobservable: not an absence proof
    work = {pid for pid, (_, ident) in procs.items() if baseline is None or ident not in baseline}
    grew = True
    while grew:  # an exempt process with a working descendant is itself working
        grew = False
        for pid, (ppid, _) in procs.items():
            if pid in work and ppid in procs and ppid not in work:
                work.add(ppid)
                grew = True
    if work:
        return Observation(POSITIVE, len(work))
    if unreadable:
        return Observation(UNKNOWN, reason=f'{unreadable} unobservable processes')
    if baseline is None:
        return Observation(UNKNOWN, reason='no trusted live baseline')
    return Observation(ZERO)


def observe_leases() -> Observation:
    from hermes_cli import worker_compute_capacity as capacity
    from hermes_cli import worker_drain_fence as fence
    try:
        busy = fence.busy_leases(capacity.policy_root())
    except (fence.DrainRefused, OSError):
        return Observation(UNKNOWN, reason='lease dir unreadable')
    return Observation(POSITIVE, busy) if busy else Observation(ZERO)


def observe_results() -> Observation:
    from hermes_cli import worker_drain_results_gate as gate
    state = gate.local_results_state()
    if state.state == gate.CLEAR:
        return Observation(ZERO)
    if state.state == gate.PENDING:
        return Observation(POSITIVE, max(state.pending, 1), state.reason)
    return Observation(UNKNOWN, reason=state.reason)


def observe_all(adapters: dict | None = None) -> dict:
    """Run the full manifest. A missing/extra adapter raises: the manifest is exact."""
    if adapters is None:
        base = load_baseline()
        adapters = {'host_processes': lambda: observe_processes(baseline=base),
                    'work_leases': observe_leases, 'local_results': observe_results}
    if set(adapters) != set(ADAPTERS):
        raise ValueError('census adapters differ from manifest')
    out = {}
    for name in ADAPTERS:
        try:
            out[name] = adapters[name]()
        except Exception as exc:  # health: allow BLE001 -- an adapter fault is unknown, never zero
            out[name] = Observation(UNKNOWN, reason=type(exc).__name__)
    return out


def manifest_count(adapters: dict | None = None) -> int | None:
    """Positive lower bound if any adapter sees work; 0 only if every adapter proves zero; else UNKNOWN."""
    seen = observe_all(adapters)
    if any(o.state == POSITIVE for o in seen.values()):
        return sum(o.count for o in seen.values() if o.state == POSITIVE)
    return 0 if all(o.state == ZERO for o in seen.values()) else None


def complete_count() -> int | None:
    """Census for the heartbeat: manifest result through the unsent-results gate."""
    from hermes_cli import machine_persistent_jobs as jobs
    return jobs.census_count(manifest_count())
