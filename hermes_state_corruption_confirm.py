"""Confirm a structural ``SQLITE_CORRUPT`` before it latches state.db for the process.

The storage latch (``hermes_state_health``) never clears, so one unconfirmed
"database disk image is malformed" used to refuse every write until a restart
(2026-11-03, hermes-hub: an hour of refused receipts and transcripts on a file that later
passed ``quick_check`` on every table). Before the writer or the pooled reader replays a
statement that raised structural corruption, this module runs a bounded
``PRAGMA quick_check`` scoped to the tables the failing attempt touched (each table's
indexes are checked with it), on a FRESH read-only connection in a FRESH process:

* fresh process: a second connection opened and closed in this process would cancel the
  POSIX locks every sibling connection holds on the file (close() drops all of the
  process's fcntl locks; https://sqlite.org/howtocorrupt.html §2.2);
* read-only (``mode=ro`` + ``query_only``): the check can never write to the file;
* bounded: the child is killed at the caller's remaining write patience (capped);
* fail closed: only an exact ``ok`` for every checked table is "clean". Damage, a timeout,
  a missing budget, a spawn failure, unparsable output or any error is not, and the caller
  latches as before.

The scope comes from a SQLite authorizer installed on the connection for the attempt
(``TouchedTables``): it sees every table each statement reads or writes at prepare time,
including trigger bodies and FTS5's internal shadow-table statements. It records table
NAMES only, never SQL text or bound parameters. When the scope is unknown (the error came
before any statement was prepared, e.g. ``BEGIN IMMEDIATE``; an action the recorder cannot
map; a table the fresh connection cannot resolve) the check widens to a whole-file
``quick_check`` under the same bound, so an unknown scope is never treated as clean.

A clean check does not prove the file was never damaged, nor explain the first error: it
says the pages those tables live on read back consistent now. The caller therefore allows
exactly one replay; a recurrence latches without a second check.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

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
        """Install this recorder as *conn*'s authorizer for the block. Installing an authorizer
        expires the connection's prepared statements, so cached statements re-prepare and are
        seen too. A connection that refuses the authorizer leaves the scope unknown."""
        try:
            conn.set_authorizer(self)
            self._attached = True
        except (sqlite3.Error, AttributeError, TypeError):  # closed / foreign connection: scope unknown
            self._unknown = True
        try:
            yield self
        finally:
            if self._attached:
                try:
                    conn.set_authorizer(None)
                except (sqlite3.Error, AttributeError, TypeError) as exc:  # closed under us: nothing to detach
                    logger.debug("state.db scope recorder: authorizer detach failed: %s", exc)

    def tables(self) -> Optional[frozenset]:
        if not self._attached or self._unknown or not self._names:
            return None
        return frozenset(self._names)


@dataclass(frozen=True)
class ConfirmResult:
    """``outcome``: ``clean`` | ``damaged`` | ``timeout`` | ``no_budget`` | ``error``.
    Only ``clean`` allows the replay."""

    outcome: str
    scope: str  # "full" or comma-separated checked table names
    elapsed_s: float
    detail: str = ""

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
import json, sqlite3, sys
uri, scope_json, busy_ms = sys.argv[1], sys.argv[2], int(sys.argv[3])
scope = json.loads(scope_json)
out = {}
try:
    conn = sqlite3.connect(uri, uri=True, timeout=busy_ms / 1000.0, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only=1")
        targets = None
        if scope is not None:
            known = {}
            for name, sql in conn.execute("SELECT name, sql FROM sqlite_schema WHERE type='table'"):
                known[name.lower()] = (name, (sql or "").lstrip().upper().startswith("CREATE VIRTUAL"))
            known["sqlite_schema"] = known["sqlite_master"] = ("sqlite_schema", False)
            targets = ["sqlite_schema"]
            for name in scope:
                hit = known.get(str(name).lower())
                if hit is None:
                    targets = None
                    out["widened"] = "unresolved table"
                    break
                if not hit[1] and hit[0] not in targets:
                    targets.append(hit[0])
        problems = []
        if targets is None:
            problems += [r[0] for r in conn.execute("PRAGMA quick_check")]
            problems = [] if problems == ["ok"] else problems
        else:
            for t in targets:
                rows = [r[0] for r in conn.execute("PRAGMA quick_check('%s')" % t.replace("'", "''"))]
                if rows != ["ok"]:
                    problems += rows
        out.update(ok=not problems, checked=targets, problems=len(problems),
                   first=[str(p)[:160] for p in problems[:3]])
    finally:
        conn.close()
except sqlite3.Error as exc:
    out = {"ok": False, "error": type(exc).__name__,
           "sqlite": getattr(exc, "sqlite_errorname", None), "message": str(exc)[:160]}
sys.stdout.write(json.dumps(out))
"""


def _child_argv(uri: str, scope_json: str, busy_ms: int) -> list[str]:
    """The fresh-process command (a seam for tests that need a hung or failing child)."""
    return [sys.executable, "-I", "-S", "-c", _CHILD, uri, scope_json, str(busy_ms)]


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


def confirm_corruption(db_path, tables: Optional[frozenset], timeout_s: float) -> ConfirmResult:
    """Bounded read-only ``quick_check`` of *tables* (``None`` = whole file) in a fresh process.

    Never raises; anything but an exact per-table ``ok`` is a non-clean result."""
    started = time.monotonic()
    requested = None if tables is None else sorted(tables)
    label = _scope_label(requested)
    if timeout_s < CONFIRM_MIN_S:
        return ConfirmResult("no_budget", label, 0.0, f"{max(timeout_s, 0.0):.3f}s left of the write patience")
    try:
        argv = _child_argv(read_only_db_uri(db_path), json.dumps(requested), int(timeout_s * 1000))
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout_s, check=False,
            close_fds=True, env=_child_env(),
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
    if "error" in out:
        detail = f"{out.get('error')}/{out.get('sqlite')}: {out.get('message', '')}"[:200]
        corrupt = out.get("sqlite") in ("SQLITE_CORRUPT", "SQLITE_NOTADB") or "malformed" in str(out.get("message"))
        return ConfirmResult("damaged" if corrupt else "error", label, elapsed, detail)
    if out.get("ok") is True and out.get("problems") == 0:
        return ConfirmResult("clean", label, elapsed, str(out.get("widened") or ""))
    first = "; ".join(str(p) for p in (out.get("first") or []))[:300]
    return ConfirmResult("damaged", label, elapsed, f"{out.get('problems')} problem(s): {first}")


__all__ = [
    "CONFIRM_MAX_S",
    "CONFIRM_MIN_S",
    "READ_CONFIRM_TIMEOUT_S",
    "ConfirmResult",
    "TouchedTables",
    "confirm_budget",
    "confirm_corruption",
]
