"""Panel-only positive retirement, backed by live per-incarnation kernel witnesses.

Enrollment alone never calls this seam. The supported panel lifecycle binds it only after
installing its legacy-start fence. A witness exists only after a literal successful all-lane
drain. Starting legacy takes the SAME stable family/execution fences under LaunchGate and
withdraws the witness first. Failed drains keep those fences; no deadline or serialized bit
can certify retirement. Other processes inspect the owning PID's actual locked description.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path

from hermes_cli.serve_producer_admission import (
    AdmissionUnavailable, LaunchReceipt, _wait_gate, launcher_generation, read_participants_strict,
)
from hermes_cli.serve_producer_census import CensusUnavailable, LinuxProcessCensus, _read_bytes
from hermes_cli.serve_producer_custody import CustodyUnsafe, FamilyCustody, _acquire_runtime, _private

# Supported launches whose code never loads dashboard plugin APIs (the only legacy panel owner).
# Classification is exact current-incarnation enrollment AND PanelParticipant/bind_panel refuse
# these roles, so a surprise plugin load in such a process fails closed instead of owning a Feed.
NON_HOSTING_ROLES = frozenset(('gateway', 'agent_startup', 'tui_gateway', 'gateway_spawner'))


def nonhosting_participant(key, participants, generation, installation):
    matching = [row for row in participants if row.key == key
                and row.installation == installation and row.generation == generation]
    return len(matching) == 1 and matching[0].role in NON_HOSTING_ROLES and _current(matching[0])


def _name(ticket):
    raw = json.dumps({'key': asdict(ticket.key), 'role': ticket.role, 'generation': ticket.generation,
                      'installation': ticket.installation}, sort_keys=True).encode('ascii')
    return 'panel-retired-' + hashlib.sha256(raw).hexdigest()


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _current(ticket):
    adapter = LinuxProcessCensus()
    return adapter.read_process(ticket.identity.pid, adapter.boot_id()).key == ticket.key


class PanelParticipant:
    """A process-local handle, not portable or reconstructible from a readiness receipt."""

    def __init__(self, runtime: Path, ticket: LaunchReceipt, *, start_lock=None):
        self.runtime = Path(runtime).resolve(strict=True)
        self.ticket = ticket
        self._lock = threading.RLock() if start_lock is None else start_lock
        self._witness = None
        self._owner = None
        self._execution = None
        self._started = False
        self._closed = False
        self._check()

    def _check(self):
        ticket = self.ticket
        if (self._closed or type(ticket) is not LaunchReceipt or ticket.identity.pid != os.getpid()
                or ticket.role == 'producer_broker' or ticket.role in NON_HOSTING_ROLES or not _current(ticket)
                or ticket not in read_participants_strict(self.runtime)
                or launcher_generation(Path(ticket.installation)) != ticket.generation):
            raise AdmissionUnavailable('panel participation identity changed')

    def begin_legacy(self, gate):
        """Called before ANY legacy work, including reload/retained callback startup."""
        with self._lock:
            self._check()
            gate.require_runtime(self.runtime)
            if self._owner is not None:
                return
            owner = FamilyCustody.acquire(self.runtime, 'panel')
            try:
                execution = FamilyCustody.acquire_execution(self.runtime, 'panel')
            except BaseException:
                owner.close()
                raise
            self._owner, self._execution = owner, execution
            self._started = True
            if self._witness is not None:
                self._witness.close()
                self._witness = None

    def retire(self, drain, *, gate=None):
        """Drain outside the launch mutex; publish only after independently probing both fences.

        The caller closes legacy admission BEFORE entering. No other legacy start may run
        without begin_legacy. `drain` must join every source/capture/watch/service lane.
        """
        import time
        from contextlib import nullcontext

        with self._lock:
            self._check()
            if self._witness is not None:
                self._witness.close()
                self._witness = None
            if drain() is not True:
                return False
            with (_wait_gate(self.runtime, time.monotonic() + 10) if gate is None else nullcontext(gate)) as held:
                held.require_runtime(self.runtime)
                self._check()
                if self._execution is not None:
                    self._execution.close()
                    self._execution = None
                if self._owner is not None:
                    self._owner.close()
                    self._owner = None
                # A descendant can retain either description. Its release is not inferred from
                # a successful thread join or parent waitpid. Refusal leaves no witness.
                if self._started:
                    owner = FamilyCustody.acquire(self.runtime, 'panel')
                    try:
                        execution = FamilyCustody.acquire_execution(self.runtime, 'panel')
                        execution.close()
                        self._started = False
                    finally:
                        owner.close()
                fd = _acquire_runtime(self.runtime, _name(self.ticket))
                try:
                    os.ftruncate(fd, 0)
                    os.write(fd, str(fd).encode('ascii'))
                except BaseException:
                    os.close(fd)
                    raise
                self._witness = FamilyCustody(fd)
                return True

    def close(self):
        """Withdraw observation only. Active source custody may never be closed as cleanup."""
        with self._lock:
            if self._started:
                raise AdmissionUnavailable('legacy still owns panel custody')
            if self._witness is not None:
                self._witness.close()
                self._witness = None
            self._closed = True


def retired_participant(record, participants, runtime, generation, installation):
    """Exact current ticket + private witness + actual PID-owned exclusive FLOCK.

    A file or descriptor number is only a locator. Neither a cached ACK, a passed bool,
    another process's inherited descriptor nor a same-inode unlocked reopen is evidence.
    The LaunchGate caller must retain it through census and successor custody.
    """
    matching = [row for row in participants if row.key == record.key
                and row.installation == installation and row.generation == generation
                and row.role != 'producer_broker']
    if len(matching) != 1:
        return False
    ticket = matching[0]
    path = Path(runtime) / (_name(ticket) + '.lock')
    try:
        info = path.stat(follow_symlinks=False)
        _private(info, directory=False)
        raw = _read_bytes(path, 64, nofollow=True)
        if not raw.isdigit() or len(raw) > 8:
            return False
        fd = int(raw)
        actual = Path(f'/proc/{record.identity.pid}/fd/{fd}').stat()
        if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
            return False
        lines = _read_bytes(Path(f'/proc/{record.identity.pid}/fdinfo/{fd}'), 8192).decode('ascii').splitlines()
        locked = any(line.startswith('lock:') and ' FLOCK ' in line and ' WRITE ' in line
                     and f' {record.identity.pid} ' in line for line in lines)
        return (locked and _current(ticket)
                and launcher_generation(Path(installation)) == generation
                and _read_bytes(path, 64, nofollow=True) == raw
                and _stamp(path.stat(follow_symlinks=False)) == _stamp(info))
    except (OSError, ValueError, CensusUnavailable, CustodyUnsafe):
        return False
