"""Who holds the SQLite write lock on state.db right now.

The "database is locked for over Ns" failure names the victim but not the holder, and the
open-descriptor scan (``hermes_state_holders``) cannot tell a reader from the one writer: every
Hermes process has the DB open. SQLite's unix VFS takes ``fcntl`` byte-range locks whose offsets
encode the lock kind.

Linux exports those locks in ``/proc/locks``. Darwin has no equivalent, so a child process runs
``F_GETLK`` on the write-class bytes. The probe must not open the db or ``-shm`` in the waiter:
on Darwin, ``close()`` of any fd for an inode drops every fcntl lock that process holds on it,
including SQLite's. ``F_GETLK`` itself does not set a lock.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# sqlite/src/os_unix.c: UNIX_SHM_BASE = (22 + SQLITE_SHM_NLOCK) * 4 = 120; WAL lock i sits at BASE + i.
_SHM_LOCK_NAMES = {
    120: "WAL write",
    121: "WAL checkpoint",
    122: "WAL recover",
    128: "shm dead-man switch",
}
# Rollback-journal / exclusive locks on the main file live in the pending-byte page (1 GiB).
_PENDING_BYTE = 0x40000000
_MAIN_LOCK_NAMES = {
    _PENDING_BYTE: "PENDING",
    _PENDING_BYTE + 1: "RESERVED",
}

# A finished probe with no holder. Unsupported platforms and a failed probe must not use this.
_RELEASED_DETAIL = (
    "no write-class lock held at the deadline (the holder released just before it)"
)
_UNKNOWN_PLATFORM = "this platform has no lock-owner diagnostic"

# F_GETLK does not block. The bound only covers a wedged interpreter startup.
_DARWIN_PROBE_TIMEOUT_S = 5.0

# Darwin struct flock is not the Linux layout: l_start, l_len, l_pid, l_type, l_whence.
# l_len must be 1. l_len 0 means "to EOF" and would match every later lock on the file.
_DARWIN_PROBE_SCRIPT = """\
import fcntl
import json
import os
import struct
import sys


def getlk(fd, start):
    buf = struct.pack("@qqihh", start, 1, 0, fcntl.F_WRLCK, os.SEEK_SET)
    out = fcntl.fcntl(fd, fcntl.F_GETLK, buf)
    _start, _length, pid, typ, _whence = struct.unpack("@qqihh", out)
    return pid, typ


def main():
    probes = json.loads(sys.argv[2])
    base = os.path.realpath(sys.argv[1])
    fds = {}
    hits = []
    try:
        for item in probes:
            sidecar = item["sidecar"]
            start = item["start"]
            if sidecar not in fds:
                try:
                    # Read-only. F_GETLK queries; it does not set a lock or write the file.
                    fds[sidecar] = os.open(base + sidecar, os.O_RDONLY)
                except FileNotFoundError:
                    fds[sidecar] = None
                except OSError as exc:
                    print(
                        json.dumps(
                            {
                                "probed": False,
                                "error": f"{sidecar or 'db'}: {exc.strerror or exc}",
                            }
                        ),
                        flush=True,
                    )
                    return
            fd = fds[sidecar]
            if fd is None:
                continue
            pid, typ = getlk(fd, start)
            if typ != fcntl.F_WRLCK:
                continue
            hits.append({"sidecar": sidecar, "start": start, "pid": pid})
    finally:
        for fd in fds.values():
            if isinstance(fd, int):
                os.close(fd)
    print(json.dumps({"probed": True, "base": base, "hits": hits}), flush=True)


main()
"""


def darwin_write_lock_probes() -> tuple[tuple[str, int], ...]:
    """Write-class bytes a Darwin probe asks ``F_GETLK`` about, in report order.

    WAL read slots (123-127) and the dead-man switch are shared locks; they never block a
    writer. The shared range at pending+2 is the same. Checkpoint, recover, and PENDING are
    included even though a normal ``BEGIN IMMEDIATE`` does not hold them.
    """
    return (
        ("-shm", 120),
        ("-shm", 121),
        ("-shm", 122),
        ("", _PENDING_BYTE),
        ("", _PENDING_BYTE + 1),
    )


def lock_owner_probe_kind(platform: str) -> str:
    """``linux``, ``darwin``, or ``""`` when this platform cannot name the holder."""
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "darwin"
    return ""


def format_write_lock_holder_detail(
    probed: bool, holders: list[str], unavailable: str = ""
) -> str:
    """Log detail. An unfinished probe must say unknown, never that the holder released."""
    if not probed:
        return f"write-lock holder unknown ({unavailable or _UNKNOWN_PLATFORM})"
    if holders:
        return " | ".join(holders)
    return _RELEASED_DETAIL


def _describe_range(sidecar: str, start: int, end: int) -> str:
    if sidecar == "-shm":
        if start in _SHM_LOCK_NAMES and end == start:
            return _SHM_LOCK_NAMES[start]
        if 123 <= start <= 127 and end == start:
            return f"WAL read slot {start - 123}"
        return f"shm bytes {start}-{end}"
    if start == end and start in _MAIN_LOCK_NAMES:
        return _MAIN_LOCK_NAMES[start]
    if start == _PENDING_BYTE + 2:
        return "SHARED range"
    return f"db bytes {start}-{end}"


def _holder_line(base: str, pid: int, kind: str, sidecar: str) -> str:
    from hermes_state_holders import describe_holder_pid

    if pid > 0:
        who = describe_holder_pid(pid)
    else:
        who = "OFD lock, owner pid not exported by the kernel"
    return f"{who} holds {kind} lock on {Path(base + sidecar).name}"


def parse_proc_locks(
    text: str, inodes: dict[tuple[int, int], str]
) -> list[tuple[int, str, str]]:
    """``(pid, lock kind, sidecar)`` for every WRITE lock on one of ``inodes``.

    ``inodes`` maps ``(st_dev, st_ino)`` to ``""`` (main file), ``"-wal"`` or ``"-shm"``. Read locks
    are dropped: they never block a writer in WAL mode. An OFD lock is reported with pid ``-1``
    (the kernel does not export its owner).
    """
    found: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        # "N: POSIX ADVISORY WRITE <pid> MAJ:MIN:INO <start> <end|EOF>"; a blocked waiter is "N: -> POSIX ...".
        if len(fields) < 8 or fields[1] == "->":
            continue
        _, _kind, _cls, access, pid_s, ident, start_s, end_s = fields[:8]
        if access != "WRITE":
            continue
        try:
            major_s, minor_s, ino_s = ident.split(":")
            dev = os.makedev(int(major_s, 16), int(minor_s, 16))
            key = (dev, int(ino_s))
            pid = int(pid_s)
            start = int(start_s)
            end = start if end_s == "EOF" else int(end_s)
        except ValueError:
            continue
        sidecar = inodes.get(key)
        if sidecar is None:
            continue
        found.append((pid, _describe_range(sidecar, start, end), sidecar))
    return found


def _linux_write_lock_holders(db_path) -> list[str]:
    base = os.path.realpath(os.fspath(db_path))
    inodes: dict[tuple[int, int], str] = {}
    for sidecar in ("", "-wal", "-shm"):
        try:
            st = os.stat(base + sidecar)
        except OSError:
            continue
        inodes[(st.st_dev, st.st_ino)] = sidecar
    # /proc/locks is host-wide and served over several read()s: lock churn in other processes
    # shifts it mid-read and can skip the holder. A skip rarely repeats, so union three passes.
    held: list[tuple[int, str, str]] = []
    for _ in range(3):
        try:
            with open("/proc/locks", encoding="ascii", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return []
        held.extend(
            entry for entry in parse_proc_locks(text, inodes) if entry not in held
        )
    return [_holder_line(base, pid, kind, sidecar) for pid, kind, sidecar in held]


def lines_from_darwin_hits(base: str, hits) -> list[str]:
    """Name write-class hits. Bytes outside the probe plan are ignored."""
    if not isinstance(hits, list):
        return []
    allowed = set(darwin_write_lock_probes())
    lines = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        sidecar = hit.get("sidecar")
        start = hit.get("start")
        pid = hit.get("pid")
        if (
            not isinstance(sidecar, str)
            or type(start) is not int
            or type(pid) is not int
        ):
            continue
        if (sidecar, start) not in allowed:
            continue
        lines.append(
            _holder_line(base, pid, _describe_range(sidecar, start, start), sidecar)
        )
    return lines


def _probe_failure_text(stderr: str, stdout: str) -> str:
    line = ""
    for part in f"{stderr}\n{stdout}".splitlines():
        part = part.strip()
        if part:
            line = part
    return (line or "probe exited nonzero")[:160]


def interpret_darwin_probe(
    returncode: int, stdout: str, stderr: str
) -> tuple[bool, list[str], str]:
    """``(probed, lines, unavailable)`` from a child probe. Failure is not an empty hold."""
    if returncode != 0:
        return (
            False,
            [],
            f"darwin lock probe failed: {_probe_failure_text(stderr, stdout)}",
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return False, [], "darwin lock probe returned no result"
    if not isinstance(payload, dict) or not payload.get("probed"):
        error = (
            payload.get("error", "unknown error")
            if isinstance(payload, dict)
            else "unknown error"
        )
        return False, [], f"darwin lock probe failed: {error}"
    base = payload.get("base")
    if not isinstance(base, str) or not base:
        return False, [], "darwin lock probe returned no result"
    return True, lines_from_darwin_hits(base, payload.get("hits")), ""


def _darwin_write_lock_holders(db_path) -> tuple[bool, list[str], str]:
    # Child, not an in-process open: Darwin close() of any fd on this inode drops every
    # fcntl lock the waiter holds, and a live Hermes process is multithreaded so this is
    # spawn+exec, not fork. The script is stdlib-only; the test runner clears PYTHONPATH.
    probes = [
        {"sidecar": sidecar, "start": start}
        for sidecar, start in darwin_write_lock_probes()
    ]
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _DARWIN_PROBE_SCRIPT,
                os.path.abspath(os.fspath(db_path)),
                json.dumps(probes),
            ],
            capture_output=True,
            text=True,
            timeout=_DARWIN_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, [], f"darwin lock probe exceeded {_DARWIN_PROBE_TIMEOUT_S:.0f}s"
    except OSError as exc:
        return False, [], f"darwin lock probe failed: {exc.strerror or exc}"
    return interpret_darwin_probe(proc.returncode, proc.stdout, proc.stderr)


def _collect_write_lock_holders(db_path) -> tuple[bool, list[str], str]:
    kind = lock_owner_probe_kind(sys.platform)
    if kind == "linux":
        return True, _linux_write_lock_holders(db_path), ""
    if kind == "darwin":
        return _darwin_write_lock_holders(db_path)
    return False, [], _UNKNOWN_PLATFORM


def state_db_write_lock_holders(db_path) -> list[str]:
    """Operator-facing lines naming the processes that hold a write-class lock on ``db_path``.

    Empty when nothing is held, this platform cannot name holders, or the probe failed.
    ``log_write_lock_holders`` distinguishes those; only a finished probe with no holder
    claims the holder released.
    """
    _probed, lines, _why = _collect_write_lock_holders(db_path)
    return lines


def log_write_lock_holders(db_path, patience_s: float) -> None:
    """One WARNING naming the write-lock holders at the moment a writer gave up waiting."""
    probed, holders, why = _collect_write_lock_holders(db_path)
    detail = format_write_lock_holder_detail(probed, holders, why)
    logger.warning(
        "state.db write lock unavailable for %.0fs (%s): %s",
        patience_s,
        Path(db_path).name,
        detail,
    )
