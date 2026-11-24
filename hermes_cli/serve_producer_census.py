"""Bounded, read-only observations for producer admission (not admission itself).

A complete collection says every enumerated same-UID incarnation was observed;
it does NOT say Python processes are unrelated or that launchers participate.
Admission must classify these records under its held launch gate, join strict
ledger/control/launcher receipts and positively retire legacy spawners. No current
serve calls this module; in particular it cannot activate external production.
"""
from __future__ import annotations

import json
import math
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MAX_PROCESSES = 8192
MAX_OBSERVATION_BYTES = 32 * 1024 * 1024
_MAX_PROC_FILE = 1024 * 1024
_MAX_LEDGER = 8 * 1024 * 1024
_IDENTITY_ENV = frozenset(('HERMES_HOME', 'HERMES_RUNTIME_DIR', 'HERMES_PROFILE',
                           'HERMES_PROCESS_SPAWN', 'PYTHONPATH', 'VIRTUAL_ENV'))


class CensusUnavailable(RuntimeError):
    """Incomplete evidence must not become an empty successful inventory."""


class IncarnationChanged(CensusUnavailable):
    """PID reuse or exec-time identity mutation prevents a coherent observation."""


@dataclass(frozen=True, order=True)
class Incarnation:
    boot_id: str
    pid: int
    start_ticks: int


@dataclass(frozen=True)
class ProcessObservation:
    identity: Incarnation
    exe: str
    cwd: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]

    @property
    def retained_bytes(self) -> int:
        strings = (self.exe, self.cwd, *self.argv,
                   *(part for pair in self.environment for part in pair))
        return (512 + sys.getsizeof(self.argv) + sys.getsizeof(self.environment)
                + sum(sys.getsizeof(pair) for pair in self.environment)
                + sum(sys.getsizeof(value) for value in strings))


@dataclass(frozen=True)
class ProcessCensus:
    boot_id: str
    started_at: float
    finished_at: float
    processes: tuple[ProcessObservation, ...]
    exited: tuple[Incarnation, ...]
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.errors


_O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_O_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_O_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)


def _read_bytes(path: Path, limit: int, *, nofollow: bool = False) -> bytes:
    """Bounded read of one REGULAR file; never blocks on the path's file type.

    A FIFO/socket/device swapped in for a ledger (before or after an lstat) must
    be refused rather than hold the caller, and with it the launch gate. The
    nonblocking open returns immediately for a FIFO with no writer; the type is
    judged on the opened descriptor, so a swap after any path check is covered.
    procfs files report S_IFREG and read normally. ``nofollow`` additionally
    refuses a symlink final component (ELOOP is an OSError, hence uncertainty).
    """
    fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_CLOEXEC | (_O_NOFOLLOW if nofollow else 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CensusUnavailable('nonregular observation input')
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    with stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise CensusUnavailable('observation byte budget')
    return value


def _bounded_names(path: Path, limit: int, *, numeric: bool = False) -> tuple[str, ...]:
    names = []
    with os.scandir(path) as entries:
        for entry in entries:
            if numeric and not (entry.name.isascii() and entry.name.isdecimal()):
                continue
            if len(names) >= limit:
                raise CensusUnavailable('process_budget' if numeric else 'pending ledger count budget')
            names.append(entry.name)
    return tuple(sorted(names))


class LinuxProcessCensus:
    """Linux adapter; explicit proc root also supports isolated synthetic fixtures.

    Never scans cwd with Path.resolve(): readlink is sufficient identity evidence
    and does not stat arbitrary/deleted foreign directories. Environment bytes are
    reduced immediately to an explicit nonsecret identity allowlist, never logged.
    """

    def __init__(self, proc_root: Path = Path('/proc')):
        self.root = Path(proc_root)

    def boot_id(self) -> str:
        value = _read_bytes(self.root / 'sys/kernel/random/boot_id', 128).decode('ascii').strip()
        if not value:
            raise CensusUnavailable('missing boot identity')
        return value

    def namespace_id(self) -> str:
        """PID numbers are meaningful only inside the same kernel PID namespace."""
        value = os.readlink(self.root / 'self/ns/pid')
        if not value.startswith('pid:[') or not value.endswith(']') or not value[5:-1].isdecimal():
            raise CensusUnavailable('invalid pid namespace')
        return value

    def pids(self) -> tuple[int, ...]:
        return tuple(sorted(int(name) for name in _bounded_names(self.root, MAX_PROCESSES, numeric=True)))

    def identity(self, pid: int, boot: str) -> Incarnation:
        raw = _read_bytes(self.root / str(pid) / 'stat', 8192)
        fields = raw.rsplit(b')', 1)[1].split()
        birth = int(fields[19])
        if birth <= 0 or int(raw.split(b' ', 1)[0]) != pid:
            raise CensusUnavailable('invalid process identity')
        return Incarnation(boot, pid, birth)

    def _uid(self, pid: int) -> int:
        status = _read_bytes(self.root / str(pid) / 'status', 65536)
        for line in status.splitlines():
            if line.startswith(b'Uid:'):
                return int(line.split()[1])
        raise CensusUnavailable('missing process uid')

    def _details(self, pid: int):
        directory = self.root / str(pid)
        exe, cwd = os.readlink(directory / 'exe'), os.readlink(directory / 'cwd')
        raw = _read_bytes(directory / 'cmdline', _MAX_PROC_FILE)
        if raw.count(b'\0') > 4096:
            raise CensusUnavailable('argument count budget')
        argv = tuple(os.fsdecode(part) for part in raw.rstrip(b'\0').split(b'\0'))
        environment = []
        for item in _read_bytes(directory / 'environ', _MAX_PROC_FILE).split(b'\0'):
            key, separator, value = item.partition(b'=')
            if separator and os.fsdecode(key) in _IDENTITY_ENV:
                if len(environment) >= 64:
                    raise CensusUnavailable('identity environment count budget')
                environment.append((os.fsdecode(key), os.fsdecode(value)))
        return exe, cwd, argv, tuple(sorted(environment))

    def read_process(self, pid: int, boot: str) -> ProcessObservation:
        identity = self.identity(pid, boot)
        details = self._details(pid)
        # exec preserves startticks: compare actual executable/argv/env/cwd too.
        # This detects observed mutation, not an atomic kernel-wide snapshot;
        # supported launch fencing and strict classification remain mandatory.
        if self._details(pid) != details or self.identity(pid, boot) != identity:
            raise IncarnationChanged('process changed while reading')
        return ProcessObservation(identity, *details)

    def _observe(self, pid, boot, processes, exited, errors):
        identity = None
        try:
            # A UID read is required even for excluded processes; denial is not absence.
            if self._uid(pid) != os.getuid():  # windows-footgun: ok — explicit Linux proc adapter
                return
            identity = self.identity(pid, boot)
            record = self.read_process(pid, boot)
            if record.identity != identity or self._uid(pid) != os.getuid():  # windows-footgun: ok — Linux adapter
                raise IncarnationChanged('process changed before observation')
            processes.append(record)
        except FileNotFoundError:
            try:
                current = self.identity(pid, boot)
            except FileNotFoundError:
                if identity is not None:
                    exited.append(identity)
                else:
                    errors.append(f'pid:{pid}:unobserved_exit')
                return
            except (OSError, ValueError, IndexError, CensusUnavailable) as exc:
                errors.append(f'pid:{pid}:{type(exc).__name__}')
                return
            label = 'IncarnationChanged' if identity is not None and current != identity else 'FileNotFoundError'
            errors.append(f'pid:{pid}:{label}')
        except (OSError, ValueError, IndexError, CensusUnavailable) as exc:
            errors.append(f'pid:{pid}:{type(exc).__name__}')

    def collect(self) -> ProcessCensus:
        started, boot = time.monotonic(), ''
        processes, exited, errors = [], [], []
        try:
            boot = self.boot_id()
            pids = self.pids()
            if len(pids) > MAX_PROCESSES:
                raise CensusUnavailable('process_budget')
            retained = 0
            for pid in pids:
                before = len(processes)
                self._observe(pid, boot, processes, exited, errors)
                if len(processes) > before:
                    retained += processes[-1].retained_bytes
                if retained > MAX_OBSERVATION_BYTES:
                    processes.pop()
                    raise CensusUnavailable('retained_byte_budget')
            if self.pids() != pids:
                errors.append('census:membership_changed')
            for record in processes:
                try:
                    if self.read_process(record.identity.pid, boot) != record:
                        errors.append(f'pid:{record.identity.pid}:final_identity_changed')
                except (OSError, ValueError, IndexError, CensusUnavailable) as exc:
                    errors.append(f'pid:{record.identity.pid}:final:{type(exc).__name__}')
            if self.boot_id() != boot:
                errors.append('census:boot_changed')
        except CensusUnavailable as exc:
            errors.append(f'census:{exc}')
        except (OSError, ValueError, IndexError) as exc:
            errors.append(f'census:{type(exc).__name__}')
        return ProcessCensus(boot, started, time.monotonic(), tuple(processes), tuple(exited), tuple(errors))


def _positive_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _valid_row(row) -> bool:
    if not isinstance(row, dict) or len(row) > 32 or any(
            not isinstance(key, str) or type(value) not in (str, int, float, bool, type(None))
            for key, value in row.items()):
        return False
    required = {'pid', 'create_time', 'purpose', 'install', 'spawner_pid',
                'spawner_create', 'registered_at', 'argv'}
    if not required <= row.keys() or type(row['pid']) is not int or row['pid'] <= 0:
        return False
    if not all(isinstance(row[key], str) and row[key] for key in ('purpose', 'install', 'argv')):
        return False
    if not all(_positive_number(row[key]) for key in ('create_time', 'registered_at')):
        return False
    for key in ('boot_start', 'spawner_boot'):
        if row.get(key) is not None and not _positive_number(row[key]):
            return False
    if any(key in row and not isinstance(row[key], str) for key in ('host', 'profile', 'hermes_home')):
        return False
    if row.get('boot_id') is not None and not isinstance(row['boot_id'], str):
        return False
    if 'isolated' in row and type(row['isolated']) is not bool:
        return False
    if row.get('port') is not None and (type(row['port']) is not int or not 0 < row['port'] < 65536):
        return False
    parent, birth = row['spawner_pid'], row['spawner_create']
    return ((parent is None and birth is None)
            or (type(parent) is int and parent > 0 and _positive_number(birth)))


def read_ledger_strict(path: Path) -> tuple[dict, ...]:
    """Read raw legacy ledger AND pending rows without quarantine/skip/repair.

    A missing ledger or malformed row is INCOMPLETE. An explicitly encoded [] is
    readable evidence only, never proof that no unregistered process exists.
    """
    try:
        first_raw = _read_bytes(path, _MAX_LEDGER, nofollow=True)
        rows = json.loads(first_raw.decode('utf-8-sig'))
        if not isinstance(rows, list) or len(rows) > MAX_PROCESSES or not all(_valid_row(row) for row in rows):
            raise CensusUnavailable('invalid ledger schema')
        pending = path.with_name(path.name + '.pending')
        try:
            names = _bounded_names(pending, MAX_PROCESSES)
        except FileNotFoundError:
            names = ()
        total = len(first_raw)
        pending_bytes = {}
        for name in names:
            candidate = pending / name
            if candidate.suffix != '.json' or candidate.is_symlink():
                raise CensusUnavailable('unknown pending ledger entry')
            raw = _read_bytes(candidate, _MAX_LEDGER - total, nofollow=True)
            total += len(raw)
            pending_bytes[name] = raw
            row = json.loads(raw.decode('utf-8-sig'))
            if not _valid_row(row):
                raise CensusUnavailable('invalid pending ledger schema')
            rows.append(row)
        # Folding moves rows from pending into main. Validate BOTH sources after
        # the cut; checking only pending names misses a continuously present row.
        if _read_bytes(path, _MAX_LEDGER, nofollow=True) != first_raw:
            raise CensusUnavailable('main ledger changed')
        if pending.exists() and _bounded_names(pending, MAX_PROCESSES) != names:
            raise CensusUnavailable('pending ledger changed')
        for name, raw in pending_bytes.items():
            if _read_bytes(pending / name, len(raw), nofollow=True) != raw:
                raise CensusUnavailable('pending ledger content changed')
        return tuple(rows)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise CensusUnavailable(f'ledger:{type(exc).__name__}') from exc
