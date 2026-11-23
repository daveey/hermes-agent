"""Account source-custody authority store (``pins.sqlite3``): inert, existing-only.

Setup is explicit and only possible before a marker exists. Runtime opens
(:func:`read`, :func:`transaction`, :func:`status`) never create, repair, or
replace the store, its directory, or the marker: they validate the marker
(``version``/``store_uuid``/``store_dev``/``store_ino``/``mode``) against the
store file identity and its ``meta`` row on every open and refuse otherwise.
Nothing here produces operations or activates custody; callers hand in fully
formed rows. No operation, unit or tombstone is ever deleted by this module.

Errors are typed: :class:`StoreBusy` (retryable, SQLite or the account barrier),
:class:`StoreUnavailable` (missing/corrupt/replaced/uncertain authority; never
retry as if transient), :class:`StoreInvalid` (bad input or identity conflict),
:class:`StaleRevision` (lost a compare-and-swap). No internal retry or sleep.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from typing import Any, Iterator, Mapping
from urllib.parse import quote
import uuid

from hermes_cli import kanban_source_barrier as barrier
from hermes_cli import kanban_source_store_schema as schema
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli.kanban_source_barrier import CustodyBusy, CustodyUnavailable, HeldCustody, hold
from hermes_cli.sqlite_safe_read import connect_tracked
from hermes_cli.worker_account_boards import _refuse_test_registration

STORE_NAME = "pins.sqlite3"
_SIDECARS = ("-wal", "-shm", "-journal")
MARKER_VERSION = 1
MARKER_MODES = ("consumer_only", "active", "disabled", "retired")
_MARKER_KEYS = {"version", "store_uuid", "store_dev", "store_ino", "mode", "capability_digest"}
_MARKER_MAX_BYTES = 4096
_FILE_MODE = 0o600
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

KINDS = ("reclaim", "park", "import")
PHASES = ("prepared", "retained", "settled")
UNIT_KINDS = ("candidate", "expand", "jobs", "opaque", "commit")
UNIT_STATES = ("pending", "retained", "published", "excluded")
_UNIT_NEXT = {
    "pending": ("pending", "retained", "published", "excluded"),
    "retained": ("retained", "published"),
    "published": ("published",),
    "excluded": ("excluded",),
}
_RUN_FIELDS = ("id", "started_at", "claim", "pid", "fingerprint")
# No command strings or full card body may be persisted (contract: operations).
_FORBIDDEN_KEYS = frozenset({"command", "cmd", "argv", "body", "prompt", "env"})
_JSON_MAX = 256 * 1024
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_UNSET: Any = object()


class StoreBusy(CustodyBusy):
    """SQLite is busy. Retryable; the store is not damaged."""


class StoreUnavailable(CustodyUnavailable):
    """Authority is missing, corrupt, replaced, not private, or uncertain."""


class StoreInvalid(ValueError):
    """Malformed input or an identity conflict. Messages never echo stored data."""


class StaleRevision(RuntimeError):
    """A compare-and-swap lost: the row changed since the caller read it."""


@dataclass(frozen=True)
class SetupResult:
    path: Path
    marker: Path
    store_uuid: str
    created: bool


@dataclass(frozen=True)
class StoreMeta:
    version: int
    store_uuid: str
    created_at: int
    mode: str


@dataclass(frozen=True)
class Operation:
    op_id: str
    lookup_key: str
    board_uuid: str
    task_id: str
    source_host: str
    original_run_json: str
    kind: str
    revision: int
    scope_json: str
    phase: str
    last_attempted: int | None
    projection_revision: int


@dataclass(frozen=True)
class Unit:
    op_id: str
    unit_id: str
    revision: int
    kind: str
    state: str
    payload_json: str | None


@dataclass(frozen=True)
class ImportRecord:
    input_key: str
    cursor_json: str
    complete: bool


def store_path() -> Path:
    """``policy_root()/source-custody/pins.sqlite3``."""
    return capacity.policy_root() / "source-custody" / STORE_NAME


def marker_path() -> Path:
    """``policy_root()/source-custody.activation.json`` (outside the store directory)."""
    return barrier._activation_marker()


# ── file validation ──────────────────────────────────────────────────────────

def _private_file(st: os.stat_result, what: str) -> None:
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise StoreUnavailable(f"source-custody {what} is not a single-link regular file")
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != _FILE_MODE:  # windows-footgun: ok (POSIX-only)
        raise StoreUnavailable(f"source-custody {what} is not private to this user")


def _lstat_at(dir_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreUnavailable(f"source-custody {name} is unreadable") from exc


def _check_sidecars(dir_fd: int) -> None:
    for suffix in _SIDECARS:
        st = _lstat_at(dir_fd, STORE_NAME + suffix)
        if st is not None:
            _private_file(st, "sqlite sidecar")


def _parse_object(raw: bytes) -> dict[str, Any]:
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(items)

    def reject(_constant):
        raise ValueError("non-finite number")

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_marker(root_fd: int) -> dict[str, Any]:
    name = marker_path().name
    try:
        raw = barrier.read_regular(name, max_bytes=_MARKER_MAX_BYTES, dir_fd=root_fd,
                                   validate=lambda st: _private_file(st, "marker"))
    except (OSError, ValueError) as exc:
        raise StoreUnavailable("source-custody marker is unavailable") from exc
    try:
        if len(raw) > _MARKER_MAX_BYTES:
            raise ValueError("oversized")
        data = _parse_object(raw)
        _validate_marker(data)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise StoreUnavailable("source-custody marker is malformed") from exc
    return data


def _validate_marker(data: Mapping[str, Any]) -> None:
    if set(data) != _MARKER_KEYS or data["version"] != MARKER_VERSION or not _is_int(data["version"]):
        raise ValueError("marker shape")
    uid = data["store_uuid"]
    if not isinstance(uid, str) or not _HEX32.fullmatch(uid):
        raise ValueError("marker uuid")
    if not _is_int(data["store_dev"]) or not _is_int(data["store_ino"]):
        raise ValueError("marker identity")
    if data["mode"] not in MARKER_MODES or data["capability_digest"] is not None:
        # capability digests belong to a later activation build
        raise ValueError("marker mode")


def _store_stat(dir_fd: int) -> os.stat_result | None:
    st = _lstat_at(dir_fd, STORE_NAME)
    if st is not None:
        _private_file(st, "store")
    return st


# ── connections ──────────────────────────────────────────────────────────────

def _is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def _map_sqlite(exc: sqlite3.Error) -> Exception:
    if _is_busy(exc):
        return StoreBusy("source-custody store is busy")
    return StoreUnavailable("source-custody store is unreadable or corrupt")


class _SQLiteIdentity:
    """K1-L pathname fence, not protection from a live same-euid adversary."""

    def __init__(self) -> None:
        self.fd = barrier.open_directory(store_path().parent)
        self.files: dict[str, tuple[int, int]] = {}
        try:
            self.identity = os.fstat(self.fd)
            self.check()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)

    def check(self) -> None:
        try:
            held = os.fstat(self.fd)
            if (not stat.S_ISDIR(held.st_mode) or held.st_uid != os.geteuid()  # windows-footgun: ok (POSIX custody)
                    or held.st_mode & 0o077):
                raise StoreUnavailable('custody SQLite parent is not private')
            current_fd = barrier.open_directory(store_path().parent)
            try:
                current = os.fstat(current_fd)
                if (current.st_dev, current.st_ino) != (self.identity.st_dev, self.identity.st_ino):
                    raise StoreUnavailable('custody SQLite parent changed')
            finally:
                os.close(current_fd)
            for name in (STORE_NAME, *(STORE_NAME + suffix for suffix in _SIDECARS)):
                st = _lstat_at(self.fd, name)
                if st is None:
                    if name == STORE_NAME or name in self.files:
                        raise StoreUnavailable('custody SQLite artifact disappeared')
                    continue
                _private_file(st, 'SQLite artifact')
                identity = (st.st_dev, st.st_ino)
                if name in self.files and self.files[name] != identity:
                    raise StoreUnavailable('custody SQLite artifact replaced')
                self.files[name] = identity
        except OSError as exc:
            raise StoreUnavailable('custody SQLite identity unavailable') from exc


def _connect(*, rw: bool, identity: _SQLiteIdentity) -> sqlite3.Connection:
    identity.check()
    uri = "file:" + quote(str(store_path())) + ("?mode=rw" if rw else "?mode=ro")
    try:
        conn = connect_tracked(uri, uri=True, timeout=0, isolation_level=None,  # custody-sqlite: a
                               wait_for_tracking=False, check_same_thread=True)
    except sqlite3.Error as exc:
        raise _map_sqlite(exc) from exc
    try:
        conn.execute("PRAGMA synchronous=FULL")
        if not rw:
            conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error as exc:
        conn.close()
        raise _map_sqlite(exc) from exc
    return conn


def _verify_connection(conn: sqlite3.Connection) -> str:
    try:
        store_uuid = schema.verify(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    except sqlite3.Error as exc:
        raise _map_sqlite(exc) from exc
    except ValueError as exc:
        raise StoreUnavailable(str(exc)) from exc
    if str(mode).lower() != "wal" or sync != 2:
        raise StoreUnavailable("source-custody store is not FULL synchronous WAL")
    return store_uuid


def _open(*, rw: bool, stack: ExitStack) -> tuple[sqlite3.Connection, dict[str, Any], _SQLiteIdentity]:
    """Validate marker + files, connect existing-only, re-check identity, return both."""
    barrier._require_posix()
    _refuse_test_registration()
    root_fd = barrier._open_policy_root(capacity.policy_root())
    dir_fd = -1
    try:
        marker = _read_marker(root_fd)
        dir_fd = barrier._open_dir(root_fd, create=False)
        _check_identity(dir_fd, marker)
        identity = _SQLiteIdentity()
        stack.callback(identity.close)
        conn = _connect(rw=rw, identity=identity)
        try:
            _check_identity(dir_fd, marker)
            if _verify_connection(conn) != marker["store_uuid"]:
                raise StoreUnavailable("source-custody store does not match its marker")
            identity.check()
        except BaseException:
            conn.close()
            raise
        return conn, marker, identity
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
        os.close(root_fd)


def _check_identity(dir_fd: int, marker: Mapping[str, Any]) -> None:
    st = _store_stat(dir_fd)
    if st is None:
        raise StoreUnavailable("source-custody store is missing")
    if (st.st_dev, st.st_ino) != (marker["store_dev"], marker["store_ino"]):
        raise StoreUnavailable("source-custody store was replaced")
    _check_sidecars(dir_fd)


# ── validation of caller input ───────────────────────────────────────────────

def _invalid(what: str) -> StoreInvalid:
    return StoreInvalid(f"invalid {what}")


def _hex32(what: str, value: Any) -> str:
    if not isinstance(value, str) or not _HEX32.fullmatch(value):
        raise _invalid(what)
    return value


def _text(what: str, value: Any, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise _invalid(what)
    return value


def _choice(what: str, value: Any, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise _invalid(what)
    return value


def _nonneg_int(what: str, value: Any) -> int:
    if not _is_int(value) or value < 0:
        raise _invalid(what)
    return value


def _json_text(what: str, value: Any, *, obj: bool = True, nullable: bool = False) -> str | None:
    """Validate JSON text and return it unchanged: the raw bytes are the record."""
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > _JSON_MAX:
        raise _invalid(what)
    try:
        parsed = _parse_object(value.encode("utf-8")) if obj else json.loads(
            value, parse_constant=lambda _c: (_ for _ in ()).throw(ValueError("nonfinite")))
    except (ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise _invalid(what) from exc
    if obj and _FORBIDDEN_KEYS & set(parsed):
        raise _invalid(what)
    return value


def lookup_key(*, board_uuid: str, db_dev: int, db_ino: int, source_host: str, task_id: str,
               kind: str, run: Mapping[str, Any]) -> str:
    """Hash of board UUID + physical DB dev/inode + host + task + kind + original run.

    ``run`` may carry only id/started_at/claim/pid/fingerprint; a missing or
    ``None`` value is the explicit ``absent`` tag, never an empty string. The
    run_uid is deliberately not a component: it is an alias, not an identity.
    """
    if not isinstance(run, Mapping) or set(run) - set(_RUN_FIELDS):
        raise _invalid("run identity")
    fields = {}
    for name in _RUN_FIELDS:
        value = run.get(name)
        if value is None:
            fields[name] = {"t": "absent"}
        elif isinstance(value, str) or _is_int(value):
            fields[name] = {"t": "value", "v": value}
        else:
            raise _invalid("run identity")
    identity = {
        "v": 1, "board_uuid": _hex32("board", board_uuid), "db_dev": _nonneg_int("db", db_dev),
        "db_ino": _nonneg_int("db", db_ino), "source_host": _text("host", source_host),
        "task_id": _text("task", task_id), "kind": _choice("kind", kind, KINDS), "run": fields,
    }
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── row access ───────────────────────────────────────────────────────────────

_OP_COLUMNS = ("op_id, lookup_key, board_uuid, task_id, source_host, original_run_json, kind,"
               " revision, scope_json, phase, last_attempted, projection_revision")
_UNIT_COLUMNS = "op_id, unit_id, revision, kind, state, payload_json"


class StoreReader:
    """Read access on one validated existing-only connection."""

    def __init__(self, conn: sqlite3.Connection, marker: Mapping[str, Any]) -> None:
        self._conn = conn
        self._mode = marker["mode"]
        self._marker = dict(marker)

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        try:
            return self._conn.execute(sql, args).fetchall()
        except sqlite3.Error as exc:
            raise _map_sqlite(exc) from exc

    def settings(self) -> dict[str, Any]:
        return {"journal_mode": str(self._query("PRAGMA journal_mode")[0][0]).lower(),
                "synchronous": self._query("PRAGMA synchronous")[0][0]}

    def meta(self) -> StoreMeta:
        version, store_uuid, created_at = self._query(
            "SELECT version, store_uuid, created_at FROM meta")[0]
        return StoreMeta(version, store_uuid, created_at, self._mode)

    def operation(self, op_id: str) -> Operation | None:
        rows = self._query(f"SELECT {_OP_COLUMNS} FROM operations WHERE op_id=?", (op_id,))
        return Operation(*rows[0]) if rows else None

    def find_operation(self, key: str) -> Operation | None:
        rows = self._query(f"SELECT {_OP_COLUMNS} FROM operations WHERE lookup_key=?", (key,))
        return Operation(*rows[0]) if rows else None

    def operations_for_task(self, board_uuid: str, task_id: str) -> list[Operation]:
        """Union across every physical input for this board UUID and task."""
        rows = self._query(f"SELECT {_OP_COLUMNS} FROM operations WHERE board_uuid=? AND task_id=?"
                           " ORDER BY op_id", (board_uuid, task_id))
        return [Operation(*r) for r in rows]

    def unit(self, op_id: str, unit_id: str) -> Unit | None:
        rows = self._query(f"SELECT {_UNIT_COLUMNS} FROM units WHERE op_id=? AND unit_id=?",
                           (op_id, unit_id))
        return Unit(*rows[0]) if rows else None

    def units(self, op_id: str) -> list[Unit]:
        rows = self._query(f"SELECT {_UNIT_COLUMNS} FROM units WHERE op_id=? ORDER BY unit_id",
                           (op_id,))
        return [Unit(*r) for r in rows]

    def all_units(self) -> list[Unit]:
        """Every unit, including opaque rows whose path was never recorded."""
        rows = self._query(f"SELECT {_UNIT_COLUMNS} FROM units ORDER BY op_id, unit_id")
        return [Unit(*r) for r in rows]

    def import_record(self, input_key: str) -> ImportRecord | None:
        rows = self._query("SELECT input_key, cursor_json, complete FROM imports WHERE input_key=?",
                           (input_key,))
        return ImportRecord(rows[0][0], rows[0][1], bool(rows[0][2])) if rows else None

    def _summary(self, task_id: str | None) -> dict[str, Any]:
        phases = dict.fromkeys(PHASES, 0)
        phases.update(dict(self._query("SELECT phase, COUNT(*) FROM operations GROUP BY phase")))
        incomplete = self._query("SELECT COUNT(*) FROM imports WHERE complete=0")[0][0]
        meta = self.meta()
        out: dict[str, Any] = {"state": "ready", "version": meta.version, "store_uuid": meta.store_uuid,
                               "mode": meta.mode, "operations": phases, "incomplete_imports": incomplete}
        if task_id is not None:
            sql = ("SELECT op_id, board_uuid, kind, phase, revision, projection_revision, last_attempted"
                   " FROM operations WHERE task_id=? ORDER BY op_id")
            out["task_id"] = task_id
            out["operations"] = [self._op_summary(r) for r in self._query(sql, (task_id,))]
        return out

    def _op_summary(self, row: tuple) -> dict[str, Any]:
        units = dict.fromkeys(UNIT_STATES, 0)
        units.update(dict(self._query("SELECT state, COUNT(*) FROM units WHERE op_id=? GROUP BY state",
                                      (row[0],))))
        keys = ("op_id", "board_uuid", "kind", "phase", "revision", "projection_revision",
                "last_attempted")
        return {**dict(zip(keys, row)), "units": units}


class StoreWriter(StoreReader):
    """Writes inside one ``BEGIN IMMEDIATE`` transaction; commit/rollback is the context's."""

    def create_operation(self, *, op_id: str, lookup_key: str, board_uuid: str, task_id: str,
                         source_host: str, original_run_json: str, kind: str, scope_json: str,
                         phase: str = "prepared", last_attempted: int | None = None,
                         ) -> tuple[Operation, bool]:
        """Insert, or replay: an existing ``lookup_key`` returns the stored row untouched."""
        row = (_hex32("op_id", op_id), _text("lookup_key", lookup_key, 256), _hex32("board", board_uuid),
               _text("task", task_id), _text("host", source_host),
               _json_text("original run", original_run_json), _choice("kind", kind, KINDS))
        scope = _json_text("scope", scope_json)
        phase = _choice("phase", phase, PHASES)
        attempted = None if last_attempted is None else _nonneg_int("last_attempted", last_attempted)
        existing = self.operation(row[0]) or self.find_operation(row[1])
        if existing is not None:
            self._same_identity(existing, row)
            return existing, False
        self._write("INSERT INTO operations(" + _OP_COLUMNS + ") VALUES(?,?,?,?,?,?,?,1,?,?,?,0)",
                    (*row, scope, phase, attempted))
        return self.operation(row[0]), True  # type: ignore[return-value]

    @staticmethod
    def _same_identity(existing: Operation, row: tuple) -> None:
        stored = (existing.op_id, existing.lookup_key, existing.board_uuid, existing.task_id,
                  existing.source_host, existing.original_run_json, existing.kind)
        # op_id may differ on a replay of the same lookup key; everything else may not.
        if stored[1:] != row[1:]:
            raise StoreInvalid("operation identity conflicts with the stored operation")

    def update_operation(self, op_id: str, expected_revision: int, *, scope_json: Any = _UNSET,
                         phase: Any = _UNSET, last_attempted: Any = _UNSET,
                         projection_revision: Any = _UNSET) -> Operation:
        """Compare-and-swap on ``revision``; unchanged input does not bump it."""
        current = self.operation(_hex32("op_id", op_id))
        if current is None:
            raise StoreInvalid("unknown operation")
        if current.revision != expected_revision:
            raise StaleRevision("operation revision is stale")
        new = {
            "scope_json": current.scope_json if scope_json is _UNSET else _json_text("scope", scope_json),
            "phase": current.phase if phase is _UNSET else _choice("phase", phase, PHASES),
            "last_attempted": current.last_attempted if last_attempted is _UNSET else (
                None if last_attempted is None else _nonneg_int("last_attempted", last_attempted)),
            "projection_revision": current.projection_revision if projection_revision is _UNSET
            else _nonneg_int("projection_revision", projection_revision),
        }
        if PHASES.index(new["phase"]) < PHASES.index(current.phase):
            raise StoreInvalid("operation phase cannot move backwards")
        if new["projection_revision"] < current.projection_revision:
            raise StoreInvalid("projection revision cannot move backwards")
        if all(getattr(current, k) == v for k, v in new.items()):
            return current
        self._write("UPDATE operations SET scope_json=?, phase=?, last_attempted=?,"
                    " projection_revision=?, revision=revision+1 WHERE op_id=? AND revision=?",
                    (*new.values(), op_id, expected_revision), expect_row=True)
        return self.operation(op_id)  # type: ignore[return-value]

    def put_unit(self, op_id: str, unit_id: str, *, kind: str, state: str, payload_json: str | None,
                 expected_revision: int | None = None) -> tuple[Unit, bool]:
        """Insert a unit (replay-safe), or CAS-advance it. Terminal states never reopen."""
        want = self._unit_args(op_id, unit_id, kind, state, payload_json)
        current = self.unit(op_id, unit_id)
        if current is None:
            if expected_revision is not None:
                raise StoreInvalid("unknown unit")
            self._write("INSERT INTO units(" + _UNIT_COLUMNS + ") VALUES(?,?,1,?,?,?)", want)
            self._bump_operation(op_id)
            return self.unit(op_id, unit_id), True  # type: ignore[return-value]
        same = (current.kind, current.state, current.payload_json) == want[2:]
        if expected_revision is None:
            if same:
                return current, False
            raise StaleRevision("unit already exists; supply its revision to change it")
        if current.kind != kind:
            raise StoreInvalid("unit kind is immutable")
        if current.revision != expected_revision:
            raise StaleRevision("unit revision is stale")
        if same:
            return current, False
        if state not in _UNIT_NEXT[current.state] or (
                current.state in ("published", "excluded") and current.payload_json != payload_json):
            raise StoreInvalid("unit state cannot change this way")
        self._write("UPDATE units SET state=?, payload_json=?, revision=revision+1"
                    " WHERE op_id=? AND unit_id=? AND revision=?",
                    (state, payload_json, op_id, unit_id, expected_revision), expect_row=True)
        self._bump_operation(op_id)
        return self.unit(op_id, unit_id), False  # type: ignore[return-value]

    def import_unit(self, op_id: str, unit_id: str, *, kind: str, state: str,
                    payload_json: str | None) -> tuple[Unit, bool]:
        """Insert only if absent. An existing unit (newer revision, tombstone) wins untouched."""
        want = self._unit_args(op_id, unit_id, kind, state, payload_json)
        current = self.unit(op_id, unit_id)
        if current is not None:
            return current, False
        self._write("INSERT INTO units(" + _UNIT_COLUMNS + ") VALUES(?,?,1,?,?,?)", want)
        self._bump_operation(op_id)
        return self.unit(op_id, unit_id), True  # type: ignore[return-value]

    def _bump_operation(self, op_id: str) -> None:
        # Counts/placement can change with every unit mutation; keep the outbox dirty.
        self._write("UPDATE operations SET revision=revision+1 WHERE op_id=?", (op_id,), expect_row=True)

    def _unit_args(self, op_id: str, unit_id: str, kind: str, state: str, payload: Any) -> tuple:
        if self.operation(_hex32("op_id", op_id)) is None:
            raise StoreInvalid("unit refers to an unknown operation")
        return (op_id, _text("unit_id", unit_id, 256), _choice("unit kind", kind, UNIT_KINDS),
                _choice("unit state", state, UNIT_STATES),
                _json_text("unit payload", payload, obj=False, nullable=True))

    def record_import(self, input_key: str, cursor_json: str, *, complete: bool,
                      expected_cursor_json: str | None = None) -> tuple[ImportRecord, bool]:
        """Insert, replay, or CAS the cursor. A complete input never regresses."""
        key = _text("input_key", input_key, 256)
        cursor = _json_text("cursor", cursor_json, obj=False)
        if not isinstance(complete, bool):
            raise _invalid("complete")
        current = self.import_record(key)
        if current is None:
            self._write("INSERT INTO imports(input_key, cursor_json, complete) VALUES(?,?,?)",
                        (key, cursor, int(complete)))
            return self.import_record(key), True  # type: ignore[return-value]
        if current.complete:
            return current, False
        if expected_cursor_json is None:
            if (current.cursor_json, current.complete) == (cursor, complete):
                return current, False
            raise StaleRevision("import cursor exists; supply it to advance")
        if current.cursor_json != expected_cursor_json:
            raise StaleRevision("import cursor is stale")
        self._write("UPDATE imports SET cursor_json=?, complete=? WHERE input_key=? AND cursor_json=?",
                    (cursor, int(complete), key, expected_cursor_json), expect_row=True)
        return self.import_record(key), False  # type: ignore[return-value]

    def _write(self, sql: str, args: tuple, *, expect_row: bool = False) -> None:
        try:
            cursor = self._conn.execute(sql, args)
        except sqlite3.IntegrityError as exc:
            raise StoreInvalid("row conflicts with an existing row") from exc
        except sqlite3.Error as exc:
            raise _map_sqlite(exc) from exc
        if expect_row and cursor.rowcount != 1:
            raise StaleRevision("row changed concurrently")


# ── public context APIs ──────────────────────────────────────────────────────

@contextmanager
def read() -> Iterator[StoreReader]:
    """One read-only snapshot on the existing store (mode=ro). No barrier needed."""
    with ExitStack() as stack:
        conn, marker, identity = _open(rw=False, stack=stack)
        stack.callback(conn.close)
        try:
            conn.execute("BEGIN")
            conn.execute("SELECT 1 FROM meta").fetchall()
            identity.check()
        except sqlite3.Error as exc:
            raise _map_sqlite(exc) from exc
        yield StoreReader(conn, marker)


@contextmanager
def transaction(*, held: HeldCustody | None = None) -> Iterator[StoreWriter]:
    """One write transaction (mode=rw, ``BEGIN IMMEDIATE``, attempted once, no sleep).

    Takes the account barrier nonblocking (:class:`CustodyBusy` when contended)
    unless the caller passes the :class:`HeldCustody` it already holds. Commits on
    clean exit, rolls everything back on any exception.
    """
    with ExitStack() as stack:
        if held is None:
            held = stack.enter_context(hold())
        barrier.validate_held(held)
        conn, marker, identity = _open(rw=True, stack=stack)
        stack.callback(conn.close)
        if marker["mode"] == "retired":
            raise StoreUnavailable("source-custody authority is retired")
        try:
            conn.execute("BEGIN IMMEDIATE")
            identity.check()
        except sqlite3.Error as exc:
            raise _map_sqlite(exc) from exc
        writer = StoreWriter(conn, marker)
        try:
            yield writer
            identity.check()
        except BaseException:
            _rollback(conn)
            raise
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback(conn)
            raise _map_sqlite(exc) from exc


def _rollback(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def status(task_id: str | None = None) -> dict[str, Any]:
    """Read-only summary: counts and per-operation metadata, never run/scope/payload text.

    ``{'state': 'absent'}`` only when neither marker nor store exists; creates nothing,
    takes no barrier and advances no cursor. Any other inconsistency raises typed.
    """
    barrier._require_posix()
    _refuse_test_registration()
    if task_id is not None:
        _text("task", task_id)
    if not _exists(marker_path()) and not _exists(store_path()):
        return {"state": "absent"}
    with read() as snap:
        return snap._summary(task_id)


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StoreUnavailable("source-custody path is unreadable") from exc
    return True


# ── explicit setup (pre-marker only) ─────────────────────────────────────────

def setup() -> SetupResult:
    """Create the private store and the ``consumer_only`` marker; idempotent when intact.

    Pre-marker only. Never deletes, overwrites, restores or repairs: an existing
    unmarked store is adopted only when it is a complete pristine v1 store; any
    other existing bytes are retained and refused as uncertain. With a marker
    present, setup returns the intact ``consumer_only`` store or refuses.
    """
    barrier._require_posix()
    _refuse_test_registration()
    if _exists(marker_path()):
        with read() as snap:
            meta = snap.meta()
        if meta.mode != "consumer_only":
            raise StoreUnavailable("source-custody setup is closed")
        return SetupResult(store_path(), marker_path(), meta.store_uuid, False)
    barrier.establish()
    with hold():
        return _setup_unmarked()


def _setup_unmarked() -> SetupResult:
    root_fd = barrier._open_policy_root(capacity.policy_root())
    dir_fd = -1
    try:
        dir_fd = barrier._open_dir(root_fd, create=False)
        if _lstat_at(root_fd, marker_path().name) is not None:
            raise StoreUnavailable("source-custody setup is closed")
        if _store_stat(dir_fd) is None:
            _create_store(dir_fd)
        store_uuid, st = _validate_unmarked(dir_fd)
        _write_marker(root_fd, store_uuid, st)
        return SetupResult(store_path(), marker_path(), store_uuid, True)
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
        os.close(root_fd)


def _create_store(dir_fd: int) -> None:
    for suffix in _SIDECARS:
        if _lstat_at(dir_fd, STORE_NAME + suffix) is not None:
            raise StoreUnavailable("source-custody sqlite sidecar exists without a store; retained")
    try:
        fd = barrier.open_regular(STORE_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                                  mode=_FILE_MODE, dir_fd=dir_fd)
    except OSError as exc:
        raise StoreUnavailable("source-custody store cannot be created") from exc
    try:
        os.fchmod(fd, _FILE_MODE)
        barrier._fsync(fd)
    finally:
        os.close(fd)
    barrier._fsync(dir_fd)
    with ExitStack() as stack:
        identity = _SQLiteIdentity()
        stack.callback(identity.close)
        conn = _connect(rw=True, identity=identity)
        stack.callback(conn.close)
        try:
            if str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() != "wal":
                raise StoreUnavailable("source-custody store cannot use WAL")
            identity.check()
            conn.execute("BEGIN IMMEDIATE")
            try:
                identity.check()
                schema.create(conn, uuid.uuid4().hex, int(time.time()))
                identity.check()
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        except sqlite3.Error as exc:
            raise _map_sqlite(exc) from exc


def _validate_unmarked(dir_fd: int) -> tuple[str, os.stat_result]:
    """An existing unmarked store must be a complete, pristine, intact v1 store."""
    st = _store_stat(dir_fd)
    if st is None:
        raise StoreUnavailable("source-custody store is missing")
    _check_sidecars(dir_fd)
    with ExitStack() as stack:
        identity = _SQLiteIdentity()
        stack.callback(identity.close)
        conn = _connect(rw=False, identity=identity)
        stack.callback(conn.close)
        try:
            store_uuid = _verify_connection(conn)
            identity.check()
            if not schema.is_pristine(conn):
                raise StoreUnavailable("unmarked source-custody store holds data; retained")
            if conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise StoreUnavailable("source-custody store failed its integrity check")
        except sqlite3.Error as exc:
            raise _map_sqlite(exc) from exc
    after = _store_stat(dir_fd)
    if after is None or (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
        raise StoreUnavailable("source-custody store was replaced during setup")
    return store_uuid, st


def _write_marker(root_fd: int, store_uuid: str, st: os.stat_result) -> None:
    """Atomic no-overwrite publish: temp file, fsync, link to the final name, fsync parent."""
    name = marker_path().name
    data = {"version": MARKER_VERSION, "store_uuid": store_uuid, "store_dev": st.st_dev,
            "store_ino": st.st_ino, "mode": "consumer_only", "capability_digest": None}
    tmp = f"{name}.tmp-{uuid.uuid4().hex}"
    fd = barrier.open_regular(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                              mode=_FILE_MODE, dir_fd=root_fd)
    try:
        os.fchmod(fd, _FILE_MODE)
        os.write(fd, json.dumps(data, sort_keys=True).encode("utf-8"))
        barrier._fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, name, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
    except FileExistsError as exc:
        raise StoreUnavailable("source-custody marker appeared during setup") from exc
    finally:
        os.unlink(tmp, dir_fd=root_fd)  # our own temp name, never the marker or store
    barrier._fsync(root_fd)
