"""Trusted capsule admission: host_repo through a registered scoped transport. [daveey]

A worktree task needs ``host_repo`` (a checkout on this dispatcher host), which only a ``local`` terminal
backend proves. A host control plane that materializes the checkout itself and runs a non-local profile
against it (a "capsule") registers that transport here::

    handle = register_transport(board_path, dispatch_owner, profiles, project_id, repo_root, spawn_fn)
    with handle.create_scope():
        kanban_db.create_task(conn, ...)   # ordinary arguments
    ...                                    # dispatch with spawn_fn=<that exact callable>
    handle.close()

Authority is live and process-local: nothing is persisted, the handle cannot be pickled or copied, and
a restarted process (or a stock dispatcher) has no registration. ``create_task`` obtains a single-use
attestation for an exact project worktree only inside ``create_scope()`` of the current context; the
capability validator still checks every other requirement. ``dispatch_refusal`` keeps every non-local
host_repo task off any dispatcher that is not the registered owner passing the registered callable.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

REFUSAL_EVENT = "capsule_admission_refused"


class CapsuleAdmissionRefused(PermissionError):
    """A registration or scope request outside the trusted capsule contract; ``code`` is stable."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


_LOCK = threading.Lock()
_REGISTRY: dict[Path, TransportHandle] = {}
_SCOPE: ContextVar[Optional[_CreateLease]] = ContextVar("hermes_kanban_capsule_scope", default=None)
_ISSUED: weakref.WeakValueDictionary[int, _Attestation] = weakref.WeakValueDictionary()


class _LiveOnly:
    __slots__ = ()

    def __reduce_ex__(self, protocol):
        raise TypeError(f"{type(self).__name__} is live process-local authority and cannot be serialized")

    def __copy__(self):
        raise TypeError(f"{type(self).__name__} cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError(f"{type(self).__name__} cannot be copied")


class _CreateLease(_LiveOnly):
    """Copies of a context share revocation when the lexical scope exits."""

    __slots__ = ("active", "handle")

    def __init__(self, handle):
        self.handle = handle
        self.active = True


class TransportHandle(_LiveOnly):
    """A live registration. Read-only bindings; :meth:`close` revokes it everywhere.

    [daveey] run27: the bindings (board, owner, profiles, project, repo, spawn, pid) are frozen once
    constructed: assignment or deletion raises ``AttributeError``. Only :meth:`close` may flip
    ``_closed``, and only to True (a closed handle is never revived).
    """

    __slots__ = ("_board", "_closed", "_owner", "_pid", "_profiles", "_project_id", "_repo", "_spawn")

    def __init__(self, board: Path, owner: str, profiles: Mapping[str, Path], project_id: str,
                 repo: Path, spawn: Callable[..., Any]):
        init = object.__setattr__
        for name, value in (("_board", board), ("_owner", owner), ("_project_id", project_id),
                            ("_repo", repo), ("_spawn", spawn),
                            ("_profiles", MappingProxyType(dict(profiles))),
                            ("_closed", False), ("_pid", os.getpid())):
            init(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError(f"TransportHandle binding {name!r} is read-only")

    def __delattr__(self, name):
        raise AttributeError(f"TransportHandle binding {name!r} is read-only")

    board_path = property(lambda self: self._board)
    dispatch_owner = property(lambda self: self._owner)
    profiles = property(lambda self: self._profiles)
    project_id = property(lambda self: self._project_id)
    repo_root = property(lambda self: self._repo)
    spawn_fn = property(lambda self: self._spawn)

    @property
    def live(self) -> bool:
        return self._pid == os.getpid() and not self._closed and _REGISTRY.get(self._board) is self

    @contextmanager
    def create_scope(self) -> Iterator[TransportHandle]:
        """Admit ``create_task`` calls in THIS context only (threads do not inherit it)."""
        if not self.live:
            raise CapsuleAdmissionRefused("capsule_unregistered", "transport handle is closed")
        if _SCOPE.get() is not None:
            raise CapsuleAdmissionRefused("capsule_scope_nested", "a capsule create scope is already active")
        lease = _CreateLease(self)
        token = _SCOPE.set(lease)
        try:
            yield self
        finally:
            lease.active = False
            _SCOPE.reset(token)

    def close(self) -> None:
        with _LOCK:
            object.__setattr__(self, "_closed", True)  # one-way; __setattr__ refuses every rebinding
            if _REGISTRY.get(self._board) is self:
                del _REGISTRY[self._board]

    def binds(self, assignee: Optional[str]) -> bool:
        """``assignee`` is registered and still resolves to the registered home."""
        if not isinstance(assignee, str) or assignee not in self._profiles:
            return False
        return _profile_home(assignee) == self._profiles[assignee]

    def owner_unchanged(self) -> bool:
        from hermes_cli.kanban_dispatch_owner import get_dispatch_owner
        try:
            return get_dispatch_owner(self._board) == self._owner
        except (OSError, ValueError, UnicodeError):
            return False

    def __repr__(self) -> str:
        return f"<TransportHandle board={str(self._board)!r} owner={self._owner!r} live={self.live}>"


class _Attestation(_LiveOnly):
    __slots__ = ("__weakref__", "assignee", "handle", "lease")

    def __init__(self, handle: TransportHandle, assignee: str):
        self.handle, self.assignee = handle, assignee
        self.lease = _SCOPE.get()


def _require_host_control_plane(board: Path) -> None:
    """Registration is for the host control plane: never a delegated child or a dispatched worker."""
    from agent.delegation_context import (
        KANBAN_WORKER_TASK_ENV, is_delegated_child_context, is_delegated_child_process_context,
    )
    from hermes_cli.kanban_db import _assert_not_delegated_child_mutation

    if (is_delegated_child_context() or is_delegated_child_process_context()
            or (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
            or (os.environ.get(KANBAN_WORKER_TASK_ENV) or "").strip()):
        raise CapsuleAdmissionRefused("capsule_host_control_plane_required",
                                      "workers and delegated children cannot register a capsule transport")
    _assert_not_delegated_child_mutation(board)


def _profile_home(name: str) -> Optional[Path]:
    from hermes_cli.profiles import resolve_profile_env
    try:
        return Path(resolve_profile_env(name)).resolve(strict=True)
    except (OSError, ValueError, RuntimeError, SystemExit):
        return None


def _bind_profiles(profiles: Mapping[str, Any]) -> dict[str, Path]:
    from hermes_cli.kanban_db import _canonical_assignee

    bound: dict[str, Path] = {}
    for name, home in dict(profiles).items():
        try:
            canonical = isinstance(name, str) and _canonical_assignee(name) == name
        except ValueError:
            canonical = False
        resolved = _profile_home(name) if canonical else None
        if resolved is None or not isinstance(home, (str, os.PathLike)) or Path(home) != resolved:
            raise CapsuleAdmissionRefused("capsule_profile_mismatch",
                                          f"{name!r} must map to its canonical resolved profile home")
        bound[name] = resolved
    if not bound:
        raise CapsuleAdmissionRefused("capsule_profile_mismatch", "no profiles registered")
    return bound


def register_transport(board_path, dispatch_owner: str, profiles: Mapping[str, Any], project_id: str,
                       repo_root, spawn_fn: Callable[..., Any]) -> TransportHandle:
    """Register the scoped transport for one board DB; returns the live handle (see module doc)."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_dispatch_owner import validate_owner

    try:
        board = Path(board_path).resolve(strict=True)
    except (OSError, TypeError) as exc:
        raise CapsuleAdmissionRefused("capsule_board_mismatch", f"board DB unavailable: {exc}") from exc
    if not board.is_file():
        raise CapsuleAdmissionRefused("capsule_board_mismatch", "board path is not a database file")
    _require_host_control_plane(board)
    try:
        owner = validate_owner(dispatch_owner)
    except ValueError as exc:
        raise CapsuleAdmissionRefused("capsule_owner_mismatch", str(exc)) from exc
    if not callable(spawn_fn) or spawn_fn is kbd._default_spawn:
        raise CapsuleAdmissionRefused("capsule_spawn_mismatch", "spawn_fn must be the transport's own callable")
    bound = _bind_profiles(profiles)
    if not isinstance(project_id, str) or not project_id or project_id != project_id.strip():
        raise CapsuleAdmissionRefused("capsule_project_mismatch", "project_id must be a nonempty id")
    repo = _canonical_dir(repo_root)
    if repo is None:
        raise CapsuleAdmissionRefused("capsule_project_mismatch", "repo_root must be a canonical directory")
    handle = TransportHandle(board, owner, bound, project_id, repo, spawn_fn)
    if not handle.owner_unchanged():
        raise CapsuleAdmissionRefused("capsule_owner_mismatch", "dispatch_owner is not the board's persisted owner")
    with _LOCK:
        existing = _REGISTRY.get(board)
        if existing is not None and not existing._closed:
            raise CapsuleAdmissionRefused("capsule_already_registered", "board already has a live transport")
        _REGISTRY[board] = handle
    return handle


def _canonical_dir(value) -> Optional[Path]:
    try:
        path = Path(value)
        return path if path.is_absolute() and path.resolve(strict=True) == path and path.is_dir() else None
    except (OSError, TypeError):
        return None


def _conn_db(conn: sqlite3.Connection) -> Optional[Path]:
    from hermes_cli.kanban_dispatch_owner import connection_db_path
    try:
        return connection_db_path(conn, validate=False)
    except (OSError, ValueError, sqlite3.Error):
        return None


def attest_create(conn: sqlite3.Connection, *, assignee: Optional[str], project_id: Optional[str],
                  project_repo: Optional[str], workspace_kind: str, workspace_path: Optional[str],
                  runs_on) -> Optional[_Attestation]:
    """``create_task``'s seam, after native project resolution: a single-use attestation, or None."""
    lease = _SCOPE.get()
    handle = lease.handle if lease is not None and lease.active else None
    if (handle is None or not handle.live or runs_on or workspace_kind != "worktree"
            or workspace_path is not None or not isinstance(assignee, str)):
        return None
    if project_id != handle.project_id or project_repo is None or project_repo != str(handle.repo_root):
        return None
    if _conn_db(conn) != handle.board_path or not handle.owner_unchanged() or not handle.binds(assignee):
        return None
    attestation = _Attestation(handle, assignee)
    with _LOCK:
        _ISSUED[id(attestation)] = attestation
    return attestation


def attestation_grants_host_repo(attestation: Any, assignee: Optional[str]) -> bool:
    """The validator's check: issued here, unused, its scope still active and bound to ``assignee``."""
    with _LOCK:
        issued = _ISSUED.pop(id(attestation), None)
    if issued is not attestation or not isinstance(attestation, _Attestation):
        return False
    handle = attestation.handle
    lease = attestation.lease
    return (attestation.assignee == assignee and lease is not None and lease.active
            and _SCOPE.get() is lease and handle.live
            and handle.owner_unchanged() and handle.binds(assignee))


def _requires_host_repo(task) -> bool:
    from hermes_cli.kanban_capabilities import _WORKSPACE_REQUIRES
    implied = _WORKSPACE_REQUIRES.get(task.workspace_kind or "scratch", ())
    return "host_repo" in set(task.requires or ()) | set(implied)


def _scope_refusal(conn: sqlite3.Connection, task, row: sqlite3.Row, assignee: str, spawn_fn) -> Optional[str]:
    db = _conn_db(conn)
    with _LOCK:
        handle = _REGISTRY.get(db) if db is not None else None
    if handle is None or not handle.live:
        return "capsule_unregistered"
    if not handle.owner_unchanged():
        return "capsule_owner_mismatch"
    if spawn_fn is None or spawn_fn is not handle.spawn_fn:
        return "capsule_spawn_mismatch"
    if task.assignee != assignee or not handle.binds(assignee):
        return "capsule_profile_mismatch"
    if task.project_id != handle.project_id:
        return "capsule_project_mismatch"
    expected = os.path.join(str(handle.repo_root), ".worktrees", task.id)
    if task.workspace_kind != "worktree" or task.workspace_path != expected:
        return "capsule_workspace_mismatch"
    # The path string is only the contract when nothing on it redirects: the registered repo is still
    # canonical and neither ``.worktrees`` nor the task's entry is a symlink (stat only, no writes).
    if (_canonical_dir(handle.repo_root) is None or os.path.islink(os.path.dirname(expected))
            or os.path.islink(expected)):
        return "capsule_workspace_mismatch"
    if "runs_on" in row.keys() and row["runs_on"] not in (None, "", "[]"):
        return "capsule_runs_on"
    return None


def _record_refusal(conn: sqlite3.Connection, task_id: str, payload: dict) -> None:
    """Audit only (never authority): one event per distinct refusal, not one per tick."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        last = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? "
                            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if last is None or last["kind"] != REFUSAL_EVENT or last["payload"] != kb._json_or_null(payload):
            kb._append_event(conn, task_id, REFUSAL_EVENT, payload)


def claim_refusal(conn: sqlite3.Connection, task_id: str, assignee: str, spawn_fn) -> Optional[str]:
    """Write-free admission decision for the row as ``conn`` sees it now: None to proceed, else a code.

    Safe inside the claim transaction (reads only; never audits). Local-backend profiles and tasks
    without host_repo always proceed (unchanged behaviour).
    """
    from hermes_cli.kanban_capabilities import profile_is_local
    from hermes_cli.kanban_db import Task

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    task = Task.from_row(row)
    if not _requires_host_repo(task):
        return None
    try:
        if profile_is_local(assignee):
            return None
    except Exception:  # an unreadable policy cannot prove locality: refuse
        logging.getLogger(__name__).warning("capsule claim: %s policy unreadable; refusing", assignee, exc_info=True)
        return "capsule_profile_policy_unavailable"
    return _scope_refusal(conn, task, row, assignee, spawn_fn)


def record_refusal(conn: sqlite3.Connection, task_id: str, assignee: str, code: str) -> None:
    """Audit a refusal (outside any claim transaction); deduplicated, never authority."""
    _record_refusal(conn, task_id, {"code": code, "assignee": assignee})


def dispatch_refusal(conn: sqlite3.Connection, task_id: str, assignee: str, spawn_fn, *,
                     dry_run: bool) -> Optional[str]:
    """``_dispatch_lane_task``'s early seam, before guards and claim: None to proceed, else a code.

    The claim repeats :func:`claim_refusal` inside its transaction, so a row, registration or profile
    that changes after this check is still refused before any claim, run or worktree.
    """
    code = claim_refusal(conn, task_id, assignee, spawn_fn)
    if code is not None and not dry_run:
        record_refusal(conn, task_id, assignee, code)
    return code


def _reset_for_tests() -> None:
    """Drop every registration (test isolation only)."""
    with _LOCK:
        for handle in _REGISTRY.values():
            object.__setattr__(handle, "_closed", True)  # same one-way flip as close()
        _REGISTRY.clear()
        _ISSUED.clear()
