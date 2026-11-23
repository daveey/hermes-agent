"""Removal and publisher consumers. Inert until a custody marker exists.

Marker absent: callers keep today's behavior. No store open, no barrier, no
marker creation. Marker present, including consumer_only: a protecting unit
whose path overlaps the target, either way, refuses the removal and the
caller's body does not run. An opaque unit or an unreadable marker or store
refuses managed source deletion. A cache proven free of Git metadata and
symlinks by a bounded walk, with no overlap, may still be removed. The store
directory, lock, and marker are never removable. This module does not push, signal, or activate a producer.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Iterator

from hermes_cli import kanban_source_barrier as barrier
from hermes_cli import kanban_source_pin_store as store
from hermes_cli import kanban_source_publication as publication

_PROTECTING = frozenset({"pending", "retained", "published"})


class RemovalRefused(Exception):
    """The removal must not run. ``reason`` is overlap, unknown, or busy."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PublishRefused(Exception):
    """The push must not run. ``reason`` comes from the publication refuser."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def marker_state() -> str:
    """``absent``, ``present``, or ``unknown``. Absence is only FileNotFoundError."""
    try:
        os.lstat(store.marker_path())
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def _contained(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False


def _overlaps(candidate: Path, protected: Path) -> bool:
    if _contained(candidate, protected) or _contained(protected, candidate):
        return True
    # A known protected path whose canonical form cannot be read is overlap.
    try:
        candidate.resolve()
        protected.resolve()
    except OSError:
        return True
    return False


def _unit_path(unit: store.Unit) -> Path | None:
    if not unit.payload_json:
        return None
    try:
        payload = json.loads(unit.payload_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw)


_CACHE_SCAN_LIMIT = 50_000
_GIT_DATA_SUFFIXES = (".bundle", ".pack", ".idx")


def _proven_cache(path: Path) -> bool:
    """No symlink, ``.git`` entry, or bare Git layout anywhere in a bounded walk.

    A walk that errors or exceeds the entry limit proves nothing.
    """
    try:
        st = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or path.name == ".git":
        return False
    if not stat.S_ISDIR(st.st_mode):
        return (stat.S_ISREG(st.st_mode) and path.name != "packed-refs"
                and not path.name.endswith(_GIT_DATA_SUFFIXES))
    seen = 0
    pending = [path]
    while pending:
        current = pending.pop()
        names = set()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _CACHE_SCAN_LIMIT or entry.name == ".git" or entry.is_symlink():
                        return False
                    if entry.name.endswith(_GIT_DATA_SUFFIXES):
                        return False
                    names.add(entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
        except OSError:
            return False
        # Any piece of a Git directory, with or without loose refs.
        if "packed-refs" in names or ("objects" in names and names & {"HEAD", "refs"}):
            return False
    return True


def recheck_removal(path: str | Path, *, repo: str | Path | None = None, kind: str = "source") -> None:
    """Ask the final veto for a detached tree, then release custody -> raises ``RemovalRefused``.

    Recursive disposal of a tree already renamed out of its workspace is unbounded, so it
    runs after this check and never under the barrier."""
    with guard_removal(path, repo=repo, kind=kind):
        pass


def _authority_paths() -> list[Path]:
    """The store directory (store and lock) and the marker: never removable."""
    return [store.store_path().parent, barrier.lock_path().parent, store.marker_path()]


def _decision(path: Path, kind: str, state: str, repo: Path | None) -> str | None:
    if state == "unknown":
        return "unknown"
    try:
        authority = _authority_paths()
    except (OSError, RuntimeError):
        return "unknown"
    for protected in authority:
        if _overlaps(path, protected):
            return "overlap"
    try:
        with store.read() as snap:
            units = snap.all_units()
    except barrier.CustodyBusy:
        return "busy"
    except (OSError, sqlite3.Error, store.StoreUnavailable, store.StoreInvalid,
            barrier.CustodyUnavailable):
        return "unknown"
    known: list[Path] = []
    opaque = False
    for unit in units:
        if unit.state == "excluded":
            continue
        if unit.kind != "opaque" and unit.state not in _PROTECTING:
            continue
        found = _unit_path(unit)
        if unit.kind == "opaque" or found is None:
            opaque = True
            continue
        known.append(found)
    targets = [path] if repo is None else [path, repo]
    for target in targets:
        for protected in known:
            if _overlaps(target, protected):
                return "overlap"
    if not opaque:
        return None
    if kind == "cache" and _proven_cache(path):
        return None
    return "unknown"


@contextmanager
def guard_removal(path: str | Path, *, repo: str | Path | None = None,
                  kind: str = "source", conn=None) -> Iterator[None]:
    """Hold the account barrier across one removal when a marker exists.

    The caller's body runs only when the removal is allowed. A refusal raises
    before the body, so a failed check cannot fall through to ``rm``. *repo*
    is a second path checked for overlap (the main repo, or the original
    location of a detached tree). Custody is never acquired inside a board
    write transaction: a caller that passes its board *conn* while one is open
    is refused ``busy``, and a later pass outside the transaction removes it.
    """
    state = marker_state()
    if state == "absent":
        yield
        return
    if conn is not None and getattr(conn, "in_transaction", False):
        raise RemovalRefused("busy")
    target = Path(path)
    repo_path = None if repo is None else Path(repo)
    cm = barrier.hold()
    try:
        cm.__enter__()
    except (barrier.CustodyBusy, barrier.CustodyUnavailable, OSError) as exc:
        raise RemovalRefused("busy" if isinstance(exc, barrier.CustodyBusy) else "unknown") from exc
    try:
        reason = _decision(target, kind, state, repo_path)
        if reason:
            raise RemovalRefused(reason)
        yield
    finally:
        cm.__exit__(None, None, None)


@contextmanager
def removal_boundary(conn, path, *, repo=None, kind='source'):
    """Custody before a single-attempt board writer, released before its hooks."""
    from hermes_cli import kanban_db as kb
    if marker_state() == 'absent':
        with kb.write_txn(conn):
            yield
        return
    with ExitStack() as custody:
        custody.enter_context(guard_removal(path, repo=repo, kind=kind, conn=conn))
        with kb.write_txn(conn, no_retry=True, boundary_done=custody.close):
            yield


def guard_publish(repo: str | Path, argv: list[str], *, remote: str | None = None) -> None:
    """Refuse a custody namespace, a mirror, or a broad refspec. Does not push.

    Refusal does not depend on activation. The target is derived from the actual
    Git argv; ``remote`` is only an optional equality check, never an override.
    """
    reason = publication.reject_publish_argv(list(argv), repo=Path(repo), remote=remote)
    if reason:
        raise PublishRefused(reason)
