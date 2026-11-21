"""Change key for the state.db answers one board's chat-links pass used (``kanban_sync_client.reconcile_links``).

A links pass reads three things from this host's state.db files that no board, replica or ledger revision covers:
session creation sources (``kanban_session_provenance._local_session_sources``), compression lineage steps
(``kanban_chat_links._compressed_away``, with whether each store exists) and which homes exist (``_local_homes``).
Until now an unchanged board was reconciled again every ``LINKS_MAX_AGE_S`` only to re-read them. state.db's
``PRAGMA data_version`` is no key for this: every chat turn on the host commits there, so it would re-arm every
pass. The key is the answers themselves.

While a pass is active (:func:`pass_reads`):

* every such read goes to the database once per pass (``memo``), bypassing the 300 s source cache in both
  directions (a positive and a missing answer alike), so a reconcile never certifies an answer older than its pass;
* the reads one board's reconcile used are recorded (``used``), and the next pass replays exactly those reads
  (:meth:`ChatReads.unchanged`) instead of reconciling when they answer the same;
* a required read whose error the pass swallows (:func:`degrade`) leaves the board without a recorded key, so it
  keeps the previous max-age retry instead of being skipped for good.

Nothing here is shared between passes or threads: the process-wide source cache keeps serving every other reader,
including this thread outside a board's reconcile or replay, and a thread that copied this context (the links
refresher) never sees the pass. [daveey]
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

_MISSING = object()


class ChatReads:
    """One links pass: ``memo`` answers each state.db read once; ``used`` is the current board's record of them."""

    def __init__(self) -> None:
        self.memo: dict[tuple, Any] = {}
        self.used: Optional[dict[tuple, Any]] = None
        self.last: Optional[dict[tuple, Any]] = None
        self.degraded = False
        self.owner = threading.get_ident()

    def recall(self, key: tuple) -> Any:
        """This pass's answer for ``key`` (recorded as used), or ``_MISSING`` when it has not been read yet."""
        value = self.memo.get(key, _MISSING)
        if value is not _MISSING:
            self.used[key] = value
        return value

    def record(self, key: tuple, value: Any) -> Any:
        self.memo[key] = self.used[key] = value
        return value

    def answer(self, key: tuple, read: Callable[[], Any]) -> Any:
        value = self.recall(key)
        return self.record(key, read()) if value is _MISSING else value

    @contextmanager
    def board(self) -> Iterator[None]:
        """Record one board's reads; :meth:`key` afterwards is what the next pass replays."""
        self.used, self.degraded, self.last = {}, False, None
        try:
            yield
            self.last = None if self.degraded else self.used
        finally:
            self.used = None

    def key(self) -> Optional[dict[tuple, Any]]:
        """The reads the board just reconciled with, or None when one of them failed (retry by age instead)."""
        return self.last

    def unchanged(self, recorded: dict[tuple, Any]) -> bool:
        """Whether every recorded read answers the same now. Reads go through this pass's memo, so boards sharing a
        session share the read, and a board that reconciles after a False answer uses these same fresh answers."""
        from hermes_cli import loop_accounting as la
        self.used, self.degraded = {}, False
        try:
            with la.pass_("sync.links.chat_key"):
                _replay(self, recorded)
            return not self.degraded and self.used == recorded
        except (OSError, sqlite3.Error, KeyError):  # KeyError: a read kind this code cannot replay
            self.degraded = True  # diagnostic reason; the False/retry policy is unchanged
            return False
        finally:
            self.used = None


_PASS: ContextVar[Optional[ChatReads]] = ContextVar("kanban_links_chat_reads", default=None)


def active() -> Optional[ChatReads]:
    """The pass recording reads on this thread right now (inside a board's reconcile or replay), else None."""
    reads = _PASS.get()
    return reads if reads is not None and reads.used is not None and reads.owner == threading.get_ident() else None


def degrade() -> None:
    """A required read failed and its error is being swallowed: this board's pass must not be recorded as a key."""
    reads = active()
    if reads is not None:
        reads.degraded = True


def degrade_unless_absent(conn: sqlite3.Connection, table: str) -> None:
    """A board read failed and is answered empty: fine for a board that predates ``table``, a failed read otherwise."""
    if active() is None:
        return
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is None:
            return
    except sqlite3.Error:
        pass
    degrade()


@contextmanager
def pass_reads() -> Iterator[ChatReads]:
    reads = ChatReads()
    token = _PASS.set(reads)
    try:
        yield reads
    finally:
        _PASS.reset(token)


def chain_step(db: sqlite3.Connection, path: Path, sid: str) -> Optional[str]:
    """The compression continuation of ``sid`` in ``db`` (``hermes_state_compression._CHAIN_STEP_SQL``)."""
    from hermes_state_compression import _CHAIN_STEP_SQL

    def read() -> Optional[str]:
        step = db.execute(_CHAIN_STEP_SQL, (sid,)).fetchone()
        return None if step is None else str(step[0])

    reads = active()
    return read() if reads is None else reads.answer(("step", str(path), sid), read)


def _replay(reads: ChatReads, recorded: dict[tuple, Any]) -> None:
    from hermes_cli.kanban_origin import _ro
    from hermes_cli.kanban_session_provenance import _local_homes, _local_session_sources
    kinds: dict[str, list[tuple]] = {"homes": [], "exists": [], "source": [], "step": []}
    for key in recorded:
        kinds[key[0]].append(key)
    for key in kinds["homes"]:
        _local_homes(key[1])
    for key in kinds["exists"]:
        reads.answer(key, Path(key[1]).exists)
    by_path: dict[tuple[str, str], list[str]] = {}
    for key in (*kinds["source"], *kinds["step"]):
        by_path.setdefault((key[0], key[1]), []).append(key[2])
    for (kind, path), sids in by_path.items():
        if kind == "source":
            _local_session_sources(sids, home=Path(path).parent)
            continue
        todo = [sid for sid in sids if reads.recall(("step", path, sid)) is _MISSING]
        if not todo:
            continue
        db = _ro(Path(path))
        try:
            for sid in todo:
                chain_step(db, Path(path), sid)
        finally:
            db.close()
