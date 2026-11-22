"""Native per-board dispatcher coordination (not a same-UID security boundary)."""
from __future__ import annotations

from hermes_cli import kanban_lockguard as _kanban_lockguard
import json
import os
from pathlib import Path
import logging
import sqlite3
import tempfile


def connect_with_identity(path, *, existing_only=False, generation_only=False, **kwargs):
    """Capture the opened inode while native archive/delete cannot relocate it.

    Ordinary clients may still connect during a tick. Such handles cannot later
    dispatch: reopen after contention clears instead of trusting a pathname.
    """
    from hermes_cli.kanban_db_connect import _dispatch_tick_lock
    from hermes_cli.kanban_generation import lifecycle_access, prepare_connection, recover, retry_open
    from hermes_cli.sqlite_safe_read import connect_tracked, _tracking_factory

    canonical = Path(path).resolve()
    # Keep guard release outside the tracking close lock, as in the previous
    # guarded-opener composition. Only sqlite open + registration need that
    # global lock; the first journal read may wait on an unrelated board writer.
    kwargs["factory"] = _kanban_lockguard.lifecycle_factory(_kanban_lockguard.guarded_factory(
        _tracking_factory(kwargs.get("factory", sqlite3.Connection))))
    timeout = kwargs.get("timeout", 5.0)

    def open_once(remaining):
        key = canonical.with_name(canonical.name + '.generation') if generation_only else canonical
        with lifecycle_access(canonical, remaining) as lock, _dispatch_tick_lock(
                key, create_parent=not generation_only) as held:
            if generation_only and not held:
                raise sqlite3.OperationalError('database is locked: board generation busy')
            recover(str(canonical))
            # Observer opens must never resurrect an archived/deleted board.
            database = canonical.as_uri() + '?mode=rw' if existing_only else str(canonical)
            options = {**kwargs, 'uri': True} if existing_only else kwargs
            conn = connect_tracked(database, wait_for_tracking=False, **options)
            conn._hermes_lifecycle_lock = lock
            try:
                prepare_connection(conn, timeout)
                stat = canonical.stat() if held else None
                conn._dispatch_identity = (stat.st_dev, stat.st_ino) if stat else None
                conn._dispatch_open_contended = not held
                return conn
            except BaseException:
                conn.close()
                raise

    return retry_open(open_once, timeout)


def connection_db_path(conn: sqlite3.Connection, *, validate: bool = True) -> Path:
    """Validate the opened inode, not just PRAGMA's original filename.

    Call again under the dispatch lock before reading ownership or doing work.
    Raw SQLite handles cannot establish lifecycle provenance and fail closed.
    """
    expected = getattr(conn, "_dispatch_identity", None)
    if validate and expected is None:
        raise ValueError("dispatch requires a native connection opened without lock contention; reconnect")
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            path = Path(row[2]).resolve(strict=True)
            stat = path.stat()
            if validate and (stat.st_dev, stat.st_ino) != expected:
                raise ValueError("database was relocated or replaced after connection open; reconnect")
            return path
    raise ValueError("dispatcher requires an identifiable on-disk database")


def metadata_path(db_path: Path) -> Path:
    # Ownership follows the DB, not board display/discovery metadata. Including
    # the filename keeps distinct DBs in one directory independent. No layout
    # heuristics: symlinked roots and built-in archive moves retain authority.
    path = Path(db_path).resolve()
    return path.with_name(path.name + ".dispatch-owner.json")


def read_metadata(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        # Absence is legacy only when no dangling path component hides data.
        if any(p.is_symlink() and not p.exists() for p in (path, *path.parents)):
            raise ValueError("dangling board metadata link")
        return {}
    if not isinstance(raw, dict):
        raise ValueError("board metadata must be an object")
    return raw


def validate_owner(owner: str) -> str:
    from hermes_cli import kanban_db as kb
    if not isinstance(owner, str) or not kb._BOARD_SLUG_RE.fullmatch(owner):
        raise ValueError("dispatch_owner must be a nonempty lowercase board slug")
    return owner


def get_dispatch_owner(db_path: Path) -> str | None:
    """Strict read: malformed/unreadable metadata must never mean unowned."""
    raw = read_metadata(metadata_path(db_path))
    return validate_owner(raw["dispatch_owner"]) if "dispatch_owner" in raw else None


def atomic_metadata_write(path: Path, raw: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".board-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(raw, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_owned_tick(conn, tick, dispatcher_id, board, dry_run):
    """All tick effects are behind the identity read, under the canonical lock."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli.kanban_db_dispatch import DispatchResult
    if getattr(conn, "_dispatch_open_contended", False):
        return DispatchResult(skipped_locked=True)
    try:
        db_path = connection_db_path(conn, validate=False)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return DispatchResult(skipped_owned=True, ownership_error=str(exc))
    with kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            return DispatchResult(skipped_locked=True)
        try:
            connection_db_path(conn)
            owner = get_dispatch_owner(db_path)
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            return DispatchResult(skipped_owned=True, ownership_error=str(exc))
        if owner is not None and dispatcher_id != owner:
            return DispatchResult(skipped_owned=True, dispatch_owner=owner)
        if not dry_run:
            from hermes_cli.kanban_model_retirement import sweep as retire_model_policy
            retire_model_policy(conn)
        from hermes_cli import kanban_ratio_routing as ratio
        try:
            ratio.begin_tick(conn)  # ratio routing re-reads this board's start counts once per tick
        except Exception:  # health: allow BLE001 -- a memo fault must not stop the tick; ratio routing fails open
            ratio.off_this_tick()
            logging.getLogger(__name__).warning("kanban dispatch: ratio routing tick start failed; default cards keep "
                                                "today's route this tick", exc_info=True)
        result = tick()
        result.dispatch_owner = owner
        kbc._maybe_checkpoint_wal(conn, db_path)
    # Denied ticks deliberately don't invoke observers, which may mutate state.
    kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    return result


def set_dispatch_owner(db_path: Path, owner: str | None) -> str | None:
    """Operator API: set exact slug, or explicitly clear with None.

    No dispatcher ID is inherited by workers. This API coordinates cooperative
    same-UID runtimes; it does not authenticate callers or authorize task work.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import _dispatch_tick_lock
    kb._assert_not_delegated_child_mutation()
    if owner is not None:
        validate_owner(owner)
    path = Path(db_path).resolve(strict=True)
    with _dispatch_tick_lock(path) as held:
        if not held:
            raise RuntimeError("board dispatcher is busy; retry owner change")
        target = metadata_path(path)
        raw = read_metadata(target)
        if owner is None:
            raw.pop("dispatch_owner", None)
        else:
            raw["dispatch_owner"] = owner
        atomic_metadata_write(target, raw)
        return get_dispatch_owner(path)
