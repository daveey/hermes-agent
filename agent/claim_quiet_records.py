"""Record schemas, strict validation and atomic file writes for the claim-quiet lease.

Every reader of these records is fail-open for admission and fail-closed for observation, so
validation here is deliberately strict: any deviation is a :class:`RecordError`, never a repair.
"""

from __future__ import annotations

import json
import math
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from hermes_platform import quiet_clock

SCHEMA = 1
MAX_LEASE_S = 480
COOLDOWN_S = 2 * 60 * 60
PRODUCER_RETRY_S = 1.0
MAX_RECORD_BYTES = 16384
MAX_REASON_CHARS = 200
LOCK_NAME = "producer.lock"
LAST_STARTED_NAME = "last-started.json"
LEASE_NAME = "lease.json"
_NS = 1_000_000_000


class RecordError(ValueError):
    """A record is missing, unreadable or not exactly the schema this code writes."""


class QuietClock(Protocol):
    def wall(self) -> float: ...
    def elapsed_ns(self) -> int | None: ...
    def boot_id(self) -> str | None: ...


class SystemClock:
    def wall(self) -> float:
        return quiet_clock.wall_time()

    def elapsed_ns(self) -> int | None:
        return quiet_clock.suspend_inclusive_ns()

    def boot_id(self) -> str | None:
        return quiet_clock.boot_id()


@dataclass(frozen=True)
class QuietLease:
    episode_id: str
    producer_pid: int
    producer_started_at: float
    producer_incarnation: str
    boot_id: str
    started_at: float
    expires_at: float
    started_ns: int
    deadline_ns: int
    max_s: int
    reason: str
    root: str
    lock_dev: int
    lock_ino: int

    def remaining_s(self, now_ns: int) -> float:
        return max(0.0, (self.deadline_ns - now_ns) / _NS)


@dataclass(frozen=True)
class LastStarted:
    episode_id: str
    started_at: float
    started_ns: int
    boot_id: str


def sample(clock: QuietClock, now: float | None = None) -> tuple[float, int, str]:
    """One strictly validated ``(wall, elapsed_ns, boot_id)`` reading, taken before any deadline or
    cooldown logic. An explicit ``now`` replaces only the wall value and is validated like it.
    Unavailable (``None``) values, wrong types, NaN/infinity, negatives and a raising clock all
    raise :class:`quiet_clock.ClockSampleError`."""
    try:
        wall = clock.wall() if now is None else now
        elapsed, boot = clock.elapsed_ns(), clock.boot_id()
    except Exception as exc:  # health: allow BLE001 -- a broken injected clock is an unusable sample
        raise quiet_clock.ClockSampleError(f"clock raised {type(exc).__name__}") from exc
    return (quiet_clock.validate_wall(wall), quiet_clock.validate_elapsed_ns(elapsed),
            quiet_clock.validate_boot_id(boot))


def _reject_constant(name: str) -> Any:
    raise RecordError(f"non-finite JSON constant {name}")


def _int(data: dict, key: str, lo: int, hi: int | None = None) -> int:
    value = data[key]
    if type(value) is not int or value < lo or (hi is not None and value > hi):
        raise RecordError(f"{key} is not an integer in range")
    return value


def _number(data: dict, key: str) -> float:
    value = data[key]
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise RecordError(f"{key} is not a finite non-negative number")
    return float(value)


def _text(data: dict, key: str, limit: int) -> str:
    value = data[key]
    if type(value) is not str or not value or len(value) > limit or not value.isprintable():
        raise RecordError(f"{key} is not bounded printable text")
    return value


def _decode(raw: bytes, keys: frozenset[str]) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeError) as exc:
        raise RecordError(f"not JSON: {exc}") from exc
    if type(data) is not dict or set(data) != keys:
        raise RecordError("unexpected record shape")
    if data["schema"] != SCHEMA or type(data["schema"]) is not int:
        raise RecordError("unsupported schema")
    return data


_LEASE_KEYS = frozenset(
    ["schema", *QuietLease.__dataclass_fields__])  # type: ignore[attr-defined]
_LAST_KEYS = frozenset(["schema", *LastStarted.__dataclass_fields__])  # type: ignore[attr-defined]


def parse_lease(raw: bytes) -> QuietLease:
    data = _decode(raw, _LEASE_KEYS)
    lease = QuietLease(
        episode_id=_text(data, "episode_id", 64),
        producer_pid=_int(data, "producer_pid", 1),
        producer_started_at=_number(data, "producer_started_at"),
        producer_incarnation=_text(data, "producer_incarnation", 64),
        boot_id=_text(data, "boot_id", 128),
        started_at=_number(data, "started_at"),
        expires_at=_number(data, "expires_at"),
        started_ns=_int(data, "started_ns", 0),
        deadline_ns=_int(data, "deadline_ns", 0),
        max_s=_int(data, "max_s", 1, MAX_LEASE_S),
        reason=_text(data, "reason", MAX_REASON_CHARS),
        root=_text(data, "root", 4096),
        lock_dev=_int(data, "lock_dev", 0),
        lock_ino=_int(data, "lock_ino", 0),
    )
    if lease.deadline_ns - lease.started_ns != lease.max_s * _NS:
        raise RecordError("monotonic deadline does not match max_s")
    if abs((lease.expires_at - lease.started_at) - lease.max_s) > 1e-3:
        raise RecordError("wall deadline does not match max_s")
    return lease


def parse_last_started(raw: bytes) -> LastStarted:
    data = _decode(raw, _LAST_KEYS)
    return LastStarted(
        episode_id=_text(data, "episode_id", 64),
        started_at=_number(data, "started_at"),
        started_ns=_int(data, "started_ns", 0),
        boot_id=_text(data, "boot_id", 128),
    )


def dump(record: Any) -> bytes:
    return json.dumps({"schema": SCHEMA, **asdict(record)}, sort_keys=True,
                      allow_nan=False).encode("utf-8")


def dump_checked(record: Any) -> bytes:
    """``dump`` plus proof the bytes parse back to exactly this record, so a producer never writes
    (or returns an owner for) a record its own reader would reject."""
    payload = dump(record)
    parse = parse_lease if isinstance(record, QuietLease) else parse_last_started
    if parse(payload) != record:
        raise RecordError("record does not round-trip through its own parser")
    return payload


def read_record(dir_fd: int, name: str) -> bytes | None:
    """Bytes of a regular, owned, size-bounded record; ``None`` when absent; RecordError otherwise."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecordError(f"{name} unreadable: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():  # windows-footgun: ok -- POSIX-only; unsupported hosts refuse before this
            raise RecordError(f"{name} is not an owned regular file")
        raw = os.read(fd, MAX_RECORD_BYTES + 1)
    except OSError as exc:
        raise RecordError(f"{name} unreadable: {exc.strerror}") from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_RECORD_BYTES:
        raise RecordError(f"{name} too large")
    return raw


def write_atomic(dir_fd: int, name: str, payload: bytes) -> None:
    """Durably publish ``payload`` as ``name``: temp file, fsync, rename, fsync the directory."""
    tmp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=dir_fd)
    try:
        try:
            view = memoryview(payload)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with suppress(OSError):  # the temp file may already be gone; the original error matters
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)
