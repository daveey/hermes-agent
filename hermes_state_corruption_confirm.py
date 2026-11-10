"""Confirm a structural ``SQLITE_CORRUPT`` before it latches state.db for the process.

The storage latch (``hermes_state_health``) never clears, so one unconfirmed
"database disk image is malformed" used to refuse every write until a restart
(2026-11-03, hermes-hub: an hour of refused receipts and transcripts on a file that later
passed ``quick_check`` on every table; that later pass does not prove the file was never
damaged, only that it read back consistent then). Before the writer or the pooled reader replays a
statement that raised structural corruption, this module runs a bounded
``PRAGMA quick_check`` scoped to the tables the failing attempt touched (each table's
indexes are checked with it), on a FRESH read-only connection in a FRESH process:

* fresh process: a second connection opened and closed in this process would cancel the
  POSIX locks every sibling connection holds on the file (close() drops all of the
  process's fcntl locks; https://sqlite.org/howtocorrupt.html §2.2);
* read-only (``mode=ro`` + ``query_only``): the check can never write to the file;
* bounded: the child is killed at the caller's remaining write patience (capped);
* only an exact ``ok`` for every checked table is "clean". Writers fail closed on all
  non-clean results. Read timeout/no_budget fails that read without latching and starts
  one background whole-file check without an execution deadline; only damage from that
  check latches. Immediate damage/errors and replay recurrence still latch as before.

The scope comes from a SQLite authorizer composed onto the connection for the attempt
(``TouchedTables`` + ``ScopedAuthorizerConnection``): it sees every table each statement reads
or writes at prepare time, including trigger bodies and FTS5's internal shadow-table statements.
It records table NAMES only, never SQL text or bound parameters. The recorder is COMPOSED with
any authorizer the caller installed (``set_authorizer`` on a ``ScopedAuthorizerConnection``
records the caller's callback; the recorder runs first and the caller's verdict is returned
unchanged), never put in its place. Python's ``sqlite3`` cannot read a connection's current
authorizer, so on any other connection class the recorder does not attach at all and the scope
stays unknown. When the scope is unknown (the error came before any statement was prepared,
e.g. ``BEGIN IMMEDIATE``; an action the recorder cannot map; a connection without the composing
slot; a table the fresh connection cannot resolve) the check widens to a whole-file
``quick_check`` under the same bound, so an unknown scope is never treated as clean.

The scoped check always adds what the authorizer cannot see:

* ``sqlite_schema``, and ``sqlite_sequence`` when present (AUTOINCREMENT updates it internally,
  with no authorizer callback);
* every shadow table of a virtual table in scope (FTS5 reads ``<name>_data`` through
  ``sqlite3_blob_open``, which never reaches the authorizer). Virtual tables themselves are not
  passed to ``quick_check``; their shadow tables are ordinary B-trees and are checked instead.

Limits, stated so nobody reads more into ``clean`` than it says:

* A SCOPED ``quick_check`` walks only the named B-trees and their indexes. It does not check the
  freelist or page ownership across B-trees (a page claimed by two trees, or a damaged freelist
  trunk, is reported only by the whole-file mode). Damage of that shape that made the first
  statement fail is not seen; if it makes the replay fail too, that recurrence latches.
* The check is one read transaction (a snapshot) in another process. Other threads of this
  process and other processes may keep writing to the file while it runs and after it returns;
  it neither blocks them nor proves anything about their writes. The latch it guards is per
  process: another process that hits the same pages runs its own confirmation.
* A clean check does not prove the file was never damaged, nor explain the first error: it says
  the pages those tables live on read back consistent at that snapshot. The caller therefore
  allows exactly one replay; a recurrence latches without a second check. A later clean
  ``quick_check`` (by hand, or after a restart) is no such proof either.
* The child is ``python -I -S`` (stdlib only) and loads no extensions, matching SessionDB
  after CJK tokenizer retirement. Unknown custom tokenizers fail closed during whole-file
  checks. Scoped checks cover shadow B-trees, not the virtual table.
* A process that has already latched the file does not run the check again (no replay is
  possible once latched, so a check could only cost a process spawn per read).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import Context, copy_context
from dataclasses import dataclass
from typing import Iterator, Optional

from hermes_state_health import STORAGE_CORRUPT, corruption_detail, storage_state
from hermes_state_holders import read_only_db_uri

logger = logging.getLogger(__name__)

# Upper bound on one confirmation, whatever patience the caller still has. The caller passes
# its remaining patience; the effective timeout is the smaller of the two.
CONFIRM_MAX_S = 30.0
# Below this there is no time to start a child and read a page: fail closed without spawning.
CONFIRM_MIN_S = 0.05
# Readers have no write patience; a sidebar poll must not hang for long on a check.
READ_CONFIRM_TIMEOUT_S = 5.0

# Authorizer action codes (sqlite3.h). arg1 / arg2 carry the table for these:
_TABLE_IN_ARG1 = frozenset({
    2,   # SQLITE_CREATE_TABLE
    9,   # SQLITE_DELETE
    11,  # SQLITE_DROP_TABLE
    18,  # SQLITE_INSERT
    20,  # SQLITE_READ (table, column)
    23,  # SQLITE_UPDATE (table, column)
    28,  # SQLITE_ANALYZE
    29,  # SQLITE_CREATE_VTABLE
    30,  # SQLITE_DROP_VTABLE
})
_TABLE_IN_ARG2 = frozenset({
    1,   # SQLITE_CREATE_INDEX (index, table)
    7,   # SQLITE_CREATE_TRIGGER (trigger, table)
    10,  # SQLITE_DROP_INDEX (index, table)
    15,  # SQLITE_DROP_TRIGGER (trigger, table)
    26,  # SQLITE_ALTER_TABLE (db, table)
})
# No table of the main schema involved (the schema table itself is always checked):
_NO_TABLE = frozenset({
    3, 4, 5, 6, 12, 13, 14, 16,  # temp index/table/trigger/view create/drop
    8, 17,                       # CREATE/DROP VIEW (reads through a view arrive as SQLITE_READ)
    19,                          # SQLITE_PRAGMA
    21,                          # SQLITE_SELECT
    22,                          # SQLITE_TRANSACTION
    31,                          # SQLITE_FUNCTION
    32,                          # SQLITE_SAVEPOINT
    33,                          # SQLITE_RECURSIVE
})
_SQLITE_OK = 0
_SCHEMA_TABLE = "sqlite_schema"


class TouchedTables:
    """Records which main-schema tables an attempt's statements touch (names only).

    ``tables()`` is the scope for the confirmation: ``None`` means unknown (widen to the whole
    file). It is unknown when the recorder never attached, saw an unmappable action or an
    attached database, or recorded no table at all."""

    __slots__ = ("_attached", "_names", "_unknown")

    def __init__(self) -> None:
        self._names: set[str] = set()
        self._unknown = False
        self._attached = False

    def __call__(self, action, arg1, arg2, dbname, _source):  # sqlite3 authorizer signature
        # Never raise and never deny: an exception here would be turned into SQLITE_DENY and
        # fail the user's statement. Anything unexpected only widens the check.
        try:
            if dbname not in (None, "main"):
                if dbname != "temp":
                    self._unknown = True
                return _SQLITE_OK
            if action in _TABLE_IN_ARG1:
                name = arg1
            elif action in _TABLE_IN_ARG2:
                name = arg2
            elif action in _NO_TABLE:
                return _SQLITE_OK
            else:
                self._unknown = True
                return _SQLITE_OK
            if isinstance(name, str) and name:
                self._names.add(name)
            else:
                self._unknown = True
        except Exception:  # health: allow BLE001 -- authorizer callback: an escaping exception becomes SQLITE_DENY and fails the user's statement; it only widens the check
            self._unknown = True
        return _SQLITE_OK

    @contextmanager
    def recording(self, conn) -> Iterator[TouchedTables]:
        """Compose this recorder into *conn*'s authorizer for the block. Installing an authorizer
        expires the connection's prepared statements, so cached statements re-prepare and are
        seen too. A connection without the composing slot (``ScopedAuthorizerConnection``) is
        left untouched, because replacing its authorizer could drop a caller's deny rule: the
        scope stays unknown (whole-file check)."""
        push = getattr(conn, "hermes_push_scope_recorder", None)
        previous = None
        if push is None:
            self._unknown = True
        else:
            try:
                previous = push(self)
                self._attached = True
            except (sqlite3.Error, AttributeError, TypeError):  # closed connection: scope unknown
                self._unknown = True
        try:
            yield self
        finally:
            if self._attached:
                try:
                    conn.hermes_pop_scope_recorder(previous)
                except (sqlite3.Error, AttributeError, TypeError) as exc:  # closed under us: nothing to detach
                    logger.debug("state.db scope recorder: authorizer detach failed: %s", exc)

    def tables(self) -> Optional[frozenset]:
        if not self._attached or self._unknown or not self._names:
            return None
        return frozenset(self._names)


class ScopedAuthorizerConnection(sqlite3.Connection):
    """``sqlite3.Connection`` whose authorizer is a composition: an optional scope recorder (never
    denies) followed by the caller's own authorizer, whose verdict is returned unchanged.

    ``set_authorizer`` keeps its public meaning (install / clear THE caller's authorizer); the
    callback is remembered so the recorder can be added and removed around it without
    overwriting it. A caller authorizer that raises still denies the statement, exactly as when
    it is installed alone."""

    _hermes_external_authorizer = None
    _hermes_scope_recorder = None

    def set_authorizer(self, authorizer_callback):
        sqlite3.Connection.set_authorizer(self, self._hermes_compose(self._hermes_scope_recorder, authorizer_callback))
        self._hermes_external_authorizer = authorizer_callback

    def hermes_push_scope_recorder(self, recorder):
        """Put *recorder* in front of the caller's authorizer; returns the recorder it replaces."""
        previous = self._hermes_scope_recorder
        sqlite3.Connection.set_authorizer(self, self._hermes_compose(recorder, self._hermes_external_authorizer))
        self._hermes_scope_recorder = recorder
        return previous

    def hermes_pop_scope_recorder(self, previous=None) -> None:
        """Remove the recorder (restoring *previous*); the caller's authorizer stays installed."""
        sqlite3.Connection.set_authorizer(self, self._hermes_compose(previous, self._hermes_external_authorizer))
        self._hermes_scope_recorder = previous

    @staticmethod
    def _hermes_compose(recorder, external):
        if recorder is None:
            return external
        if external is None:
            return recorder

        def composed(action, arg1, arg2, dbname, source):
            recorder(action, arg1, arg2, dbname, source)  # never raises, always SQLITE_OK
            return external(action, arg1, arg2, dbname, source)

        return composed


def writer_replay_blocker(db, stage: str, first_corrupt: Optional[BaseException]) -> Optional[str]:
    """None when a write on *db* (a ``SessionDB``) that just raised structural corruption may be
    replayed once on its writer connection; otherwise why not (it becomes part of the latch reason).

    Replay only when nothing of the attempt can be durable: the error came from BEGIN or the
    callback (never COMMIT, whose settlement is unknown), the rollback really ended the
    transaction, this call has not replayed already, and the file is not latched already. The
    writer connection's unpinned page cache is released first so the replay re-reads the pages
    from the file instead of a cached copy.

    Takes the writer lock on purpose: ``in_transaction`` and ``PRAGMA shrink_memory`` are state of
    the WRITER connection itself (not a data read a pooled reader could answer), and the
    connection may only be used under that lock. Lives here, next to the confirmation it gates,
    rather than on ``SessionDB``."""
    if first_corrupt is not None:
        return f"recurred on replay (first: {corruption_detail(first_corrupt)})"
    if storage_state(db.db_path) == STORAGE_CORRUPT:
        return "no replay: storage already latched"
    if stage not in ("begin", "callback"):
        return f"no replay: stage={stage} (settlement unknown)"
    with db._lock:
        conn = db._conn
        if conn is None or conn.in_transaction:
            return "no replay: rollback not confirmed"
        try:
            conn.execute("PRAGMA shrink_memory")
        except sqlite3.Error as exc:
            return f"no replay: page-cache release failed ({exc})"
    return None


def release_reader_cache(conn) -> bool:
    """Release a pooled reader's unpinned page cache after a structural read error; False when that fails.

    The read twin of the writer's release in ``writer_replay_blocker``: a ``mode=ro`` reader keeps its
    cache across transactions while the WAL is unchanged, so a replay could re-read the cached page
    that raised instead of the file. The connection stays open and in the pool (``close()`` would
    cancel this process's POSIX locks for every sibling connection); a failure means the caller must
    not replay."""
    try:
        conn.execute("PRAGMA shrink_memory")
    except sqlite3.Error:
        return False
    return True


@dataclass(frozen=True)
class ConfirmResult:
    """``outcome``: ``clean`` | ``damaged`` | ``timeout`` | ``no_budget`` | ``error``.
    Only ``clean`` allows the replay."""

    outcome: str
    scope: str  # "full" or comma-separated checked table names
    elapsed_s: float
    detail: str = ""
    table_results: tuple[tuple[str, str], ...] = ()

    @property
    def clean(self) -> bool:
        return self.outcome == "clean"

    def describe(self) -> str:
        text = f"confirm={self.outcome} scope={self.scope} {self.elapsed_s:.2f}s"
        return f"{text} ({self.detail})" if self.detail else text


# Runs in the fresh process (``python -I -S``: no site-packages, no PYTHON* env, stdlib only).
# argv: uri, JSON list of table names or null (whole file), busy timeout in ms.
# Prints one JSON object; problem rows are page/row locations, never row contents.
_CHILD = r"""
import json, os, sqlite3, sys, threading
uri, scope_json, busy_ms = sys.argv[1], sys.argv[2], int(sys.argv[3])
parent_pid = int(sys.argv[4])

# Detach from the caller's foreground process group, not from its lifetime. A restarted
# POSIX gateway must not leave minutes-long read snapshots holding back WAL reclamation.
def watch_parent():
    tick = threading.Event()
    while os.getppid() == parent_pid:
        tick.wait(1.0)
    os._exit(1)

if os.name == "posix":
    threading.Thread(target=watch_parent, daemon=True).start()

scope = json.loads(scope_json)
out = {}
try:
    conn = sqlite3.connect(uri, uri=True, timeout=busy_ms / 1000.0, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=1")

        targets = None
        if scope is not None:
            known, virtual = {}, []
            for name, sql in conn.execute("SELECT name, sql FROM sqlite_schema WHERE type='table'"):
                is_virtual = (sql or "").lstrip().upper().startswith("CREATE VIRTUAL")
                known[name.lower()] = (name, is_virtual)
                if is_virtual:
                    virtual.append(name.lower())
            known["sqlite_schema"] = known["sqlite_master"] = ("sqlite_schema", False)
            if not scope:  # explicit diagnostic request: every ordinary/shadow table
                scope = sorted(k for k, (_, vt) in known.items() if not vt)
            targets = ["sqlite_schema"]
            if "sqlite_sequence" in known:  # AUTOINCREMENT writes it with no authorizer callback
                targets.append(known["sqlite_sequence"][0])
            wanted = []
            for name in scope:
                key = str(name).lower()
                if key not in known:
                    targets = None
                    out["widened"] = "unresolved table"
                    break
                wanted.append(key)
                # A virtual table (or one of its shadow tables) in scope brings all of its shadow
                # tables: FTS5 reads <name>_data through the blob API, invisible to the authorizer.
                for vt in virtual:
                    if key == vt or key.startswith(vt + "_"):
                        wanted += sorted(k for k in known if k.startswith(vt + "_") and not known[k][1])
            if targets is not None:
                for key in wanted:
                    real, is_virtual = known[key]
                    if not is_virtual and real not in targets:
                        targets.append(real)
        out["checked"] = targets  # kept when a check below raises: the latch names what was checked
        problems = []
        if targets is None:
            problems += [r[0] for r in conn.execute("PRAGMA quick_check")]
            problems = [] if problems == ["ok"] else problems
        else:
            for t in targets:
                try:
                    rows = [r[0] for r in conn.execute("PRAGMA quick_check('%s')" % t.replace("'", "''"))]
                except sqlite3.Error as exc:
                    rows = ["%s/%s: %s" % (type(exc).__name__, getattr(exc, "sqlite_errorname", None), str(exc)[:160])]
                    if out.get("sqlite") not in ("SQLITE_CORRUPT", "SQLITE_NOTADB"):
                        out.update(error=type(exc).__name__, sqlite=getattr(exc, "sqlite_errorname", None), message=str(exc)[:160])
                out.setdefault("table_results", []).append([t, "; ".join(str(r)[:160] for r in rows[:3])])
                if rows != ["ok"]:
                    problems += rows
        out.update(ok=not problems, checked=targets, problems=len(problems),
                   first=[str(p)[:160] for p in problems[:3]])
    finally:
        conn.close()
except sqlite3.Error as exc:
    out = {"ok": False, "error": type(exc).__name__,
           "sqlite": getattr(exc, "sqlite_errorname", None), "message": str(exc)[:160],
           **({"checked": out["checked"]} if "checked" in out else {})}
sys.stdout.write(json.dumps(out))
"""


def _child_argv(uri: str, scope_json: str, busy_ms: int) -> list[str]:
    """The fresh-process command (a seam for tests that need a hung or failing child)."""
    return [sys.executable, "-I", "-S", "-c", _CHILD, uri, scope_json, str(busy_ms), str(os.getpid())]


def confirm_budget(deadline: Optional[float], *, default_s: float = READ_CONFIRM_TIMEOUT_S) -> float:
    """Seconds the confirmation may take: the caller's remaining patience (or *default_s*), capped."""
    remaining = default_s if deadline is None else deadline - time.monotonic()
    return max(0.0, min(remaining, CONFIRM_MAX_S))


def _child_env() -> dict[str, str]:
    """The stdlib-only child needs no profile home, credentials or PYTHON* settings (``-I``
    ignores the latter anyway): pass only what an interpreter needs to start."""
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    for name in ("SYSTEMROOT", "TMPDIR", "TEMP", "TMP"):  # SYSTEMROOT: Windows CPython startup
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _scope_label(checked) -> str:
    if checked is None:
        return "full"
    return ",".join(sorted("".join(ch for ch in str(n) if ch.isalnum() or ch == "_")[:64] for n in checked))[:400]


def confirm_corruption(db_path, tables: Optional[frozenset], timeout_s: Optional[float]) -> ConfirmResult:
    """Read-only ``quick_check`` in a fresh process; timeout None is background-only.

    tables=None checks the whole file; an empty frozenset requests all per-table results.
    Never loads extensions; anything but an exact per-table ``ok`` is non-clean."""
    started = time.monotonic()
    requested = None if tables is None else sorted(tables)
    label = _scope_label(requested)
    if timeout_s is not None and timeout_s < CONFIRM_MIN_S:
        return ConfirmResult("no_budget", label, 0.0, f"{max(timeout_s, 0.0):.3f}s left of the write patience")
    try:
        argv = _child_argv(read_only_db_uri(db_path), json.dumps(requested),
                           int((timeout_s if timeout_s is not None else 30.0) * 1000))
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, check=False,
            close_fds=True, env=_child_env(), start_new_session=timeout_s is None,
        )
    except subprocess.TimeoutExpired:
        return ConfirmResult("timeout", label, time.monotonic() - started, f"killed after {timeout_s:.2f}s")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:  # spawn failure (no executable, EMFILE, ...)
        return ConfirmResult("error", label, time.monotonic() - started, f"spawn: {type(exc).__name__}")
    elapsed = time.monotonic() - started
    try:
        out = json.loads(proc.stdout.decode("utf-8", "replace") or "null")
    except ValueError:
        out = None
    if proc.returncode != 0 or not isinstance(out, dict):
        return ConfirmResult("error", label, elapsed, f"child rc={proc.returncode}, unparsable output")
    if "checked" in out:
        label = _scope_label(out.get("checked"))
    try:
        results = tuple((str(t), str(r)) for t, r in out.get("table_results", []))
    except (ValueError, TypeError):
        return ConfirmResult("error", label, elapsed, "invalid per-table results")
    if "error" in out:
        detail = f"{out.get('error')}/{out.get('sqlite')}: {out.get('message', '')}"[:200]
        corrupt = out.get("sqlite") in ("SQLITE_CORRUPT", "SQLITE_NOTADB") or "malformed" in str(out.get("message"))
        return ConfirmResult("damaged" if corrupt else "error", label, elapsed, detail, results)
    if out.get("ok") is True and out.get("problems") == 0:
        return ConfirmResult("clean", label, elapsed, str(out.get("widened") or ""), results)
    if out.get("ok") is not False or not isinstance(out.get("problems"), int) or out["problems"] <= 0:
        return ConfirmResult("error", label, elapsed, "invalid check result")
    first = "; ".join(str(p) for p in (out.get("first") or []))[:300]
    return ConfirmResult("damaged", label, elapsed, f"{out.get('problems')} problem(s): {first}", results)


_background_lock = threading.Lock()
_background_thread: Optional[threading.Thread] = None
_background_paths: dict[str, tuple[str, Context]] = {}


def background_confirmation_pending(db_path) -> bool:
    with _background_lock:
        return os.path.realpath(db_path) in _background_paths


def _run_background_confirmations() -> None:
    global _background_thread
    from hermes_state_health import mark_storage_corrupt

    while True:
        with _background_lock:
            if not _background_paths:
                _background_thread = None
                return
            path, (first_error, context) = next(iter(_background_paths.items()))
        try:
            logger.warning("state.db read corrupt unconfirmed; background quick_check started: %s; %s",
                           path, first_error)
            result = context.run(confirm_corruption, path, None, None)
            logger.warning("state.db background quick_check result: %s; %s", path, result.describe())
            if result.outcome == "damaged":
                context.run(mark_storage_corrupt, path, f"{first_error}; background {result.describe()}")
        except Exception:  # health: allow BLE001 -- a diagnostic failure is not evidence of damage; keep draining other profiles
            logger.exception("state.db background quick_check failed: %s", path)
        finally:
            with _background_lock:
                _background_paths.pop(path, None)


def schedule_background_confirmation(db_path, first_error: str) -> bool:
    """One whole-file child in flight per process. Queue distinct files, coalesce same-file scopes.

    No execution deadline: only actual damage latches. Further reads of a queued/in-flight file
    fail immediately without spawning another foreground check. Other profile files are queued,
    not silently discarded. A subsequent incident after completion may request a new check.
    """
    global _background_thread
    path = os.path.realpath(db_path)
    with _background_lock:
        if path in _background_paths:
            return False
        _background_paths[path] = (first_error, copy_context())
        if _background_thread is not None:
            logger.warning("state.db read corrupt unconfirmed; background quick_check queued: %s", path)
            return True
        thread = threading.Thread(target=_run_background_confirmations, name="state-db-confirm", daemon=True)
        _background_thread = thread
        try:
            thread.start()
        except RuntimeError:
            _background_thread = None
            _background_paths.pop(path, None)
            logger.warning("state.db read corrupt unconfirmed; background quick_check could not start", exc_info=True)
            return False
        return True


__all__ = [
    "CONFIRM_MAX_S",
    "CONFIRM_MIN_S",
    "READ_CONFIRM_TIMEOUT_S",
    "ConfirmResult",
    "ScopedAuthorizerConnection",
    "TouchedTables",
    "confirm_budget",
    "confirm_corruption",

    "writer_replay_blocker",
]
