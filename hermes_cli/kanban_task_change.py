"""Per-task change cursor on a board or sync shadow (P2, t_01a1d59d3ef805c4). [daveey]

The activity-revision triggers (``kanban_activity_revision``) upsert ``kanban_task_change(task_id, rev)`` with the
revision each content write bumped to. ``changed_since(conn, cursor)`` answers "which tasks moved since ``cursor``" from
the rev index, at a cost that does not grow with the board, so a reader that keeps per-task results can refresh only
those tasks. A task id whose row is gone was deleted. Bookkeeping writes (claims, leases, heartbeats) do not mark: they
do not bump the revision either (``BOOKKEEPING_COLUMNS``, ``UNREAD_EVENT_KINDS``).

Writer precondition: do not use ``INSERT OR REPLACE`` (including SQLite's ``REPLACE INTO`` shorthand) on watched
rows when a conflict can delete a row belonging to another task. With ``recursive_triggers`` off, SQLite silently
deletes the conflicting row without firing its DELETE trigger. For example, replacing task a's comment uid with
a comment for task b marks only b, although a lost a comment. The checks below do NOT detect that missing task and
do NOT force ``FULL``. The same-task replacement test does not establish safety for a cross-task unique-key conflict.
Use an ``ON CONFLICT DO UPDATE`` upsert instead: its UPDATE trigger marks both OLD and NEW task keys. A writer that
must use REPLACE needs separately verified delete-trigger coverage or explicit full invalidation; a reader's pragma
cannot enable another connection's delete triggers. Enabling recursive triggers globally also changes sync
DELETE-trigger/outbox/tombstone behavior and is not part of this cursor's contract.

Subject to that writer precondition, a cursor is trusted only when all of these hold in the reader's snapshot;
otherwise the answer is ``FULL``:

- the file carries every trigger of epoch >= 2 for the tables it watches, and the change table (``_covered``);
- (a) ``MAX(kanban_task_change.rev) == kanban_activity_revision.revision``. An older writer's init replaces our
  triggers with bump-only ones (and bumps): the counter then runs ahead of the change table;
- (b) the revision is not below the cursor's (a database restored in place: same inode, regressed counter);
- (c) the file's (dev, ino), its ``-shm`` identity and ``schema_version`` equal the cursor's, as in ``board_rows``
  (a dropped trigger moves ``schema_version`` before any unmarked write can happen);
- no ``FULL_MARK`` row is newer than the cursor (a trigger (re)install: what changed in the gap is unknown).

``FULL`` comes with a fresh cursor whenever the file can vouch for the next call (``None`` otherwise), so one full read
re-arms the incremental path.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from hermes_cli import kanban_activity_revision as rev


class _Full:
    __slots__ = ()

    def __repr__(self) -> str:
        return "FULL"


FULL = _Full()


@dataclass(frozen=True)
class Cursor:
    dev: int
    ino: int
    shm: Optional[tuple]
    schema_version: int
    revision: int
    tables: tuple[str, ...]


Changed = frozenset | _Full

_STATE_SQL = (f"SELECT r.revision, (SELECT MAX(rev) FROM {rev.CHANGE_TABLE}) FROM {rev.REVISION_TABLE} r "
              "WHERE r.id = 1")
_SINCE_SQL = f"SELECT task_id FROM {rev.CHANGE_TABLE} WHERE rev > ?"

# (path, dev, ino, tables) -> schema_version at which the triggers covered ``tables`` (one sqlite_master scan per
# schema change, like ``kanban_activity_revision._COVERED``).
_COVERED: dict[tuple, int] = {}
_COVERED_LOCK = threading.Lock()
_COVERED_MAX = 256


def _main_path(conn: sqlite3.Connection) -> Optional[str]:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or None
    return None


def _identity(path: str) -> tuple:
    from hermes_cli.kanban_lockguard import shm_identity
    st = os.stat(path)
    return (st.st_dev, st.st_ino, shm_identity(path))


def _scan_covered(conn: sqlite3.Connection, tables: tuple[str, ...]) -> bool:
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('table', 'trigger')")
    names: set[str] = set()
    triggers: dict[str, str] = {}
    for kind, name, _tbl, sql in rows:
        if kind == "trigger":
            triggers[name] = sql
        else:
            names.add(name)
    if not {rev.REVISION_TABLE, rev.CHANGE_TABLE} <= names:
        return False
    for table in tables:
        if table not in names:
            continue  # absent table: nothing to watch
        for op in ("insert", "update", "delete"):
            if rev.trigger_epoch(triggers.get(f"{rev.PREFIX}{table}_{op}")) < rev.EPOCH:
                return False  # missing, or an older writer's bump-only trigger
    return True


def _covered(conn: sqlite3.Connection, memo_key: tuple, schema: int, tables: tuple[str, ...]) -> bool:
    with _COVERED_LOCK:
        if _COVERED.get(memo_key) == schema:
            return True
    ok = _scan_covered(conn, tables)
    with _COVERED_LOCK:
        if not ok:
            _COVERED.pop(memo_key, None)
        else:
            if len(_COVERED) >= _COVERED_MAX:
                _COVERED.clear()
            _COVERED[memo_key] = schema
    return ok


def changed_since(conn: sqlite3.Connection, cursor: Optional[Cursor], *,
                  tables: Optional[Iterable[str]] = None) -> tuple[Changed, Optional[Cursor]]:
    """``(task ids changed after cursor, or FULL; the cursor for the next call, or None)``. ``tables`` defaults to
    the set the file's writers watch (``tables_for``). Raises ``sqlite3.Error`` like any read."""
    path = _main_path(conn)
    if path is None:
        return FULL, None
    tables = tuple(tables) if tables is not None else rev.tables_for(path)
    try:
        before = _identity(path)
    except OSError:
        return FULL, None
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")  # one snapshot: coverage, counter, MAX(rev) and the changed rows agree
    try:
        schema = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        if not _covered(conn, (path, before[0], before[1], tables), schema, tables):
            return FULL, None
        row = conn.execute(_STATE_SQL).fetchone()
        if row is None or row[1] is None or int(row[0]) != int(row[1]):
            return FULL, None  # (a): something bumped without marking
        revision = int(row[0])
        fresh = Cursor(before[0], before[1], before[2], schema, revision, tables)
        if (cursor is None or (cursor.dev, cursor.ino, cursor.shm, cursor.schema_version, cursor.tables)
                != (fresh.dev, fresh.ino, fresh.shm, fresh.schema_version, fresh.tables)
                or revision < cursor.revision):  # (c), (b)
            changed: Changed = FULL
        else:
            ids = frozenset(r[0] for r in conn.execute(_SINCE_SQL, (cursor.revision,)))
            changed = FULL if rev.FULL_MARK in ids else ids
    finally:
        if own:
            conn.execute("COMMIT")
    try:
        if _identity(path) != before:
            return FULL, None  # replaced or a new WAL generation while reading: which file was read is unknown
    except OSError:
        return FULL, None
    return changed, fresh


def _reset_for_tests() -> None:
    with _COVERED_LOCK:
        _COVERED.clear()
