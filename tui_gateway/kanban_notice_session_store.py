"""The chat store a remote Kanban notice pass reads and writes. [daveey]

Reads (session row, compression tip, archive/move, worker transcript, gateway lease, the notice already in the
transcript) run on the process's kept read-only ``state.db`` handle (``hermes_state_kept_reader``): no writer
open and no schema reconcile per link per pass. The one write a pass may make, the notice row and its outcome
refresh, borrows the shared writer (``hermes_state_registry.acquire``) for just that write.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Iterator, Optional


class NoticeSessionStore:
    """Read-only ``SessionDB`` facade with :meth:`notice_writer` for the write. Attribute reads go to the kept
    read-only handle; a write method called on it directly fails there (read-only), never silently elsewhere."""

    def __init__(self, reader: Any, path: Path) -> None:
        self._reader = reader
        self._path = path

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reader, name)

    @contextlib.contextmanager
    def notice_writer(self) -> Iterator[Any]:
        from hermes_state_registry import acquire, release

        db = acquire(self._path)
        try:
            yield db
        finally:
            release(db)


def notice_writer(db: Any):
    """The handle to write a notice row through: the store's shared writer, else *db* itself (a writable
    ``SessionDB`` or a test double)."""
    opener = getattr(db, "notice_writer", None) if isinstance(db, NoticeSessionStore) else None
    return opener() if opener is not None else contextlib.nullcontext(db)


@contextlib.contextmanager
def session_store(path: Path, sid: str) -> Iterator[tuple[Optional[NoticeSessionStore], Optional[str]]]:
    """``(store, compression tip)`` for chat *sid* in the ``state.db`` at *path*; ``(None, None)`` when the file or
    the session is missing."""
    if not path.is_file():
        yield None, None
        return
    from hermes_state_kept_reader import read_only

    with read_only(path) as reader:
        store = NoticeSessionStore(reader, path)
        yield (store, store.get_compression_tip(sid) or sid) if store.get_session(sid) else (None, None)


def reader_of(db: Any) -> Any:
    """The kept read-only handle behind a :class:`NoticeSessionStore`, else *db* itself. Caches keyed on the reader
    (``notice_candidates``) must see the same object every pass, not a fresh facade. [daveey]"""
    return db._reader if isinstance(db, NoticeSessionStore) else db


@contextlib.contextmanager
def profile_store(path: Path) -> Iterator[Optional[NoticeSessionStore]]:
    """The store of a foreign profile's ``state.db`` at *path* for a notice pass: the kept read-only handle, with
    no writer open and no schema reconcile per pass; writes go through :func:`notice_writer`. ``None`` when the
    file does not exist (a pass never creates another profile's store)."""
    if not path.is_file():
        yield None
        return
    from hermes_state_kept_reader import read_only

    with read_only(path) as reader:
        yield NoticeSessionStore(reader, path)
