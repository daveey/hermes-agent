"""Per-task decoded own-row inputs of ``blocker_states`` (G2c, P2 cache). [daveey]

``blocker_states`` walks the run history of every task that asserted a shared blocker in the 14-day lookback: on the
hub board ~1.4k runs of ~100 tasks, two SQL statements and a metadata decode per task, three times a sweep pass. Each
task's slice of those inputs (its runs decoded into :class:`Run` tuples, the asserting rows the fence and window exit
read, and its status) is a pure function of that task's own rows plus the shared-blocker alias snapshot, so it is kept
across passes per board file and re-read only for the tasks ``kanban_task_change.changed_since`` names.

What is cached, and what is not:

- cached per task: run id, outcome, started/ended epochs, the run's effect and blocker id, the explicit-clear flag,
  whether its metadata matches the assertion scan's ``LIKE``, and for those rows the asserting-row fields (provider,
  model and lane text, first summary line); the task's status. An alias change re-reads everything (``FULL``);
- never cached: the window (callers pass different ``now``), the global walk, ``states`` membership and
  ``blocker_provider``, route reads, condition_met evidence, ``apply_check_clears`` and the fences. No time or
  liveness decision is cached.

Window: the cache holds every task with a matching run that ended at or after its fill cutoff (``now - lookback`` at
the fill). A caller whose ``now - lookback`` is earlier than that cutoff gets a full read, so a held set is always a
superset of any window it answers. The cutoff is refreshed by a full read once it trails by more than
:data:`REFILL_SECONDS`, which bounds memory to tasks with recent assertions.

Snapshot: ``BEGIN; changed_since; reads; COMMIT`` on the caller's connection, so the cursor and the rows come from one
snapshot. A connection already inside a transaction (a caller's write or read transaction) reads afresh and never
stores. ``FULL`` follows the trust rules of ``changed_since`` (R7); a ``None`` cursor is never stored. The
``(cursor, tasks)`` pair is swapped immutably under a lock and never moves backwards on the same file.
"""
from __future__ import annotations

import sqlite3
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, NamedTuple, Optional

if TYPE_CHECKING:
    from hermes_cli.kanban_task_change import Cursor

ASSERT_LIKE = "metadata LIKE '%\"blocker_id\"%'"
# The fill cutoff trails ``now - lookback`` by at most this much before a full read drops tasks whose assertions
# all left every window a later caller can ask about.
REFILL_SECONDS = 6 * 3600
_MAX_FILES = 16
_CHUNK = 400


class AssertRow:
    """One run whose metadata matches the assertion scan, in the row shape ``_note_window_exit`` and the fence
    read (``row["ended_at"]`` ...). Immutable, so lanes share it without copying."""

    __slots__ = (
        "bid",
        "effect",
        "ended",
        "ended_at",
        "id",
        "lane",
        "metadata",
        "model",
        "outcome",
        "provider",
        "reason",
        "started",
        "started_at",
        "summary",
        "task_id",
        "uid",
    )
    _KEYS = ("id", "task_id", "started_at", "ended_at", "outcome", "metadata", "summary")

    def __init__(self, **values):
        for name in self.__slots__:
            object.__setattr__(self, name, values[name])

    def __setattr__(self, name, value):
        raise AttributeError("AssertRow is immutable")

    def __getitem__(self, key):
        if key not in self._KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def keys(self):
        return self._KEYS

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __repr__(self) -> str:
        return f"AssertRow(id={self.id}, task_id={self.task_id!r}, ended_at={self.ended_at!r})"


class Run(NamedTuple):
    id: int
    outcome: Optional[str]
    started: int            # epoch, 0 when unset
    ended_raw: object       # the stored ended_at (None while the run is open)
    ended: int              # epoch, 0 when unset
    effect: str
    bid: Optional[str]
    explicit_clear: bool    # the run's ``clear_blocker`` attestation (``_explicit_clear``)
    assert_row: Optional[AssertRow]   # set when the metadata matches the assertion scan and the run ended


class TaskRows(NamedTuple):
    status: Optional[str]   # None: the task row is gone
    runs: tuple[Run, ...]


class Frozen(tuple):
    """A tuple that deep-copies to itself (its members are immutable)."""

    def __deepcopy__(self, memo):
        return self


def foreign_among(conn, ids) -> frozenset[str]:
    """The ids in ``ids`` that ``kanban_wake_check.foreign_ids`` would name (cards another gateway created).

    G2c: the blocker lanes only ask about the cards parked behind a wall. ``foreign_ids`` scans all of ``tasks``
    and its memo is keyed on the content revision a dirty tick moves, so each lane paid a full scan (~2 ms on the
    hub board); this reads only the asked rows by primary key, in the caller's snapshot."""
    from hermes_cli import kanban_wake_check as kwc

    ids = sorted(set(ids))
    own = kwc.own_gateway(conn) if ids else None
    if not own:
        return frozenset()
    out: set[str] = set()
    try:
        for i in range(0, len(ids), _CHUNK):
            part = ids[i:i + _CHUNK]
            marks = ",".join("?" * len(part))
            out.update(r[0] for r in conn.execute(
                f"SELECT id FROM tasks WHERE id IN ({marks}) AND created_on IS NOT NULL AND created_on != ?",
                (*part, own)))
    except sqlite3.OperationalError:   # no sync columns: like ``foreign_ids``, nothing is foreign
        return frozenset()
    return frozenset(out)


@dataclass(frozen=True)
class _State:
    cursor: Cursor
    aliases: object
    cutoff: int
    tasks: Mapping[str, TaskRows]


_LOCK = threading.Lock()
_STATES: dict[str, _State] = {}


def _lookback() -> int:
    from hermes_cli.kanban_shared_blockers import _LOOKBACK_SECONDS
    return _LOOKBACK_SECONDS


def sql_ge(value, bound: int) -> bool:
    """SQLite's ``value >= bound`` for an integer bound: text and blobs sort after every number."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value >= bound
    return True


def _alias_snapshot():
    """The pass's alias snapshot (what ``normalize_blocker_id`` decodes with), or None when unknown."""
    from hermes_cli.config_backend import ConfigBackendUnavailable
    from hermes_cli.kanban_blocker_inputs import _aliases, config
    try:
        return deepcopy(_aliases(config()))
    except ConfigBackendUnavailable:
        return None


def _decode(rows, statuses: Mapping[str, Optional[str]]) -> dict[str, TaskRows]:
    """``rows``: (id, task_id, started_at, ended_at, outcome, metadata, summary, uid, matches) ordered by task, id."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_shared_blockers import _explicit_clear, run_effect

    out: dict[str, list] = {}
    for rid, tid, started_at, ended_at, outcome, metadata, summary, uid, matches in rows:
        effect, bid = run_effect(outcome, metadata)
        explicit = _explicit_clear(kb, metadata)
        started = kb._to_epoch(started_at) or 0
        ended = (kb._to_epoch(ended_at) or 0) if ended_at is not None else 0
        arow = None
        if matches and ended_at is not None:
            meta = kb._json_dict(metadata)
            provider = meta.get("blocker_provider")
            model, lane = meta.get("blocker_model"), meta.get("blocker_lane")
            arow = AssertRow(
                id=int(rid), task_id=tid, started_at=started_at, ended_at=ended_at, outcome=outcome,
                metadata=metadata, summary=summary, uid=uid, effect=effect, bid=bid, started=started, ended=ended,
                provider=provider if isinstance(provider, str) else None,
                model=model.strip() or None if isinstance(model, str) else None,
                lane=lane.strip() or None if isinstance(lane, str) else None,
                reason=kb._first_line(summary, 300))
        out.setdefault(tid, []).append(Run(int(rid), outcome, started, ended_at, ended, effect, bid, explicit, arow))
    return {tid: TaskRows(statuses.get(tid), tuple(runs)) for tid, runs in out.items()}


_HELD_SQL = f"SELECT task_id FROM task_runs WHERE ended_at IS NOT NULL AND ended_at >= ? AND {ASSERT_LIKE}"


def _run_cols(conn) -> str:
    # Legacy unsynced schemas have no run uid (``kanban_shared_blocker_checks``: task/time identity instead).
    uid = "uid" if any(r[1] == "uid" for r in conn.execute("PRAGMA table_info(task_runs)")) else "NULL AS uid"
    return f"id, task_id, started_at, ended_at, outcome, metadata, summary, {uid}, {ASSERT_LIKE} AS m"


def _read_all(conn, cutoff: int) -> dict[str, TaskRows]:
    rows = conn.execute(f"SELECT {_run_cols(conn)} FROM task_runs WHERE task_id IN ({_HELD_SQL}) "
                        "ORDER BY task_id, id", (cutoff,)).fetchall()
    statuses = dict(conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({_HELD_SQL})", (cutoff,)).fetchall())
    return _decode(rows, statuses)


def _read_tasks(conn, ids) -> dict[str, TaskRows]:
    ids = sorted(ids)
    cols = _run_cols(conn)
    rows: list = []
    statuses: dict = {}
    for i in range(0, len(ids), _CHUNK):
        part = ids[i:i + _CHUNK]
        marks = ",".join("?" * len(part))
        rows += conn.execute(f"SELECT {cols} FROM task_runs WHERE task_id IN ({marks}) ORDER BY task_id, id",
                             part).fetchall()
        statuses.update(conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({marks})", part).fetchall())
    return _decode(rows, statuses)


def _held(entry: TaskRows, cutoff: int) -> bool:
    return any(r.assert_row is not None and sql_ge(r.ended_raw, cutoff) for r in entry.runs)


def _main_path(conn) -> Optional[str]:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return file or None
    return None


def _store(path: str, new: _State) -> None:
    with _LOCK:
        old = _STATES.get(path)
        if old is not None:
            a, b = old.cursor, new.cursor
            same = (a.dev, a.ino, a.shm, a.schema_version) == (b.dev, b.ino, b.shm, b.schema_version)
            if same and b.revision < a.revision:
                return  # a slower reader of an older snapshot: never move backwards
        elif len(_STATES) >= _MAX_FILES:
            _STATES.pop(next(iter(_STATES)))
        _STATES[path] = new


def task_rows(conn, now: int, *, cache: bool = True) -> Mapping[str, TaskRows]:
    """Decoded rows of every task with a matching run ended at or after ``now - lookback`` (possibly more)."""
    from hermes_cli.kanban_task_change import changed_since

    cutoff = int(now) - _lookback()
    aliases = _alias_snapshot() if cache else None
    if not cache or aliases is None or conn.in_transaction:
        return _read_all(conn, cutoff)
    path = _main_path(conn)
    if path is None:
        return _read_all(conn, cutoff)
    with _LOCK:
        held = _STATES.get(path)
    if held is not None and (held.aliases != aliases or cutoff < held.cutoff
                             or cutoff - held.cutoff > REFILL_SECONDS):
        held = None
    conn.execute("BEGIN")
    try:
        changed, cursor = changed_since(conn, held.cursor if held is not None else None)
        if held is None or not isinstance(changed, frozenset):  # FULL
            tasks, fill = _read_all(conn, cutoff), cutoff
        else:
            fill = held.cutoff
            tasks = held.tasks   # G2e: shared as is while nothing changed (``inputs`` keeps its sorted rows per mapping)
            if changed:
                tasks = dict(tasks)
                fresh = _read_tasks(conn, changed)
                for tid in changed:
                    entry = fresh.get(tid)
                    if entry is not None and _held(entry, fill):
                        tasks[tid] = entry
                    else:
                        tasks.pop(tid, None)
    finally:
        conn.execute("COMMIT")
    if cursor is not None:
        _store(path, _State(cursor, aliases, fill, tasks))
    return tasks


def inputs(conn, now: int, *, cache: bool = True) -> tuple[Frozen, Mapping[str, TaskRows]]:
    """``(asserting rows in the window sorted by run id, task rows)``: the in-window slice of the assertion scan
    (``ended_at >= now - lookback`` and the metadata ``LIKE``), rebuilt every call from the held rows."""
    tasks = task_rows(conn, now, cache=cache)
    bound = int(now) - _lookback()
    return Frozen(a for a, raw in _assert_rows(tasks) if sql_ge(raw, bound)), tasks


_SORTED: tuple = (None, ())   # (the task mapping, its ``(assert row, stored ended_at)`` pairs in run-id order)


def _assert_rows(tasks: Mapping[str, TaskRows]) -> tuple:
    """G2e: every held assertion row of ``tasks`` sorted by run id, kept for the mapping it was built from. A pass
    asks five times (``inputs``) and ``task_rows`` hands out the same held mapping until a task changes, so the
    walk over every run and the sort ran once per ask instead of once per change. The window is applied per ask."""
    global _SORTED
    held = _SORTED
    if held[0] is tasks:
        return held[1]
    pairs = tuple(sorted(((r.assert_row, r.ended_raw) for entry in tasks.values() for r in entry.runs
                          if r.assert_row is not None), key=lambda p: p[0].id))
    _SORTED = (tasks, pairs)
    return pairs


def _reset_for_tests() -> None:
    global _SORTED
    _SORTED = (None, ())
    with _LOCK:
        _STATES.clear()
