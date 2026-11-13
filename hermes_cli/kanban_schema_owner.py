"""Only a home's installed core may change the schema of that home's Kanban boards. [daveey]

Incident (2026-11-12, David-MBP 23:53:41 PST, daveey-mini 23:58:38 PST): a checkout of origin/main
(95fed77843) launched against the live ``~/.hermes`` while the installed core was 2cc7101849. Its first
board connect added ``tasks.review_required``, ``task_events.origin_gateway`` and the ``admission_*``
columns to the live ``kanban.db``. Three seconds later each host's gateway found columns its core does not
declare (``kanban_sync_client.SchemaAhead``) and gave up the Board sync role until that host's own core
updated, which took hours.

The installed core of a home is the checkout its launcher runs: ``<home>/hermes-agent`` (where
``hermes-core-python`` and the ``hermes`` launcher resolve it). :func:`decide` is the one predicate every
migration entry point asks (``kanban_db_connect.connect``/``init_db``, boot home maintenance):

* the installed core itself, or any tree at the same commit (a prebuilt env of that HEAD), migrates as before;
* any other checkout (a worktree, a scratch clone, a stage/prebuild tree, an env at another HEAD) never runs
  the board's schema pass. When the board already has every table and column that checkout declares, it opens
  normally. When the board lacks some, the connection opens read-only and every write fails with
  :class:`BoardSchemaNotOwned`, whose one-line message names the installed core and suggests a scratch home.
* a home with no recognizable installed core (temporary test homes, pip/Docker layouts) keeps today's
  behaviour: nothing here can say which core owns it.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

__all__ = [
    "BoardSchemaNotOwned", "Owner", "decide", "home_of_board", "installed_core", "missing_schema",
    "open_read_only", "refusal_line", "running_core",
]


class BoardSchemaNotOwned(sqlite3.OperationalError):
    """A write to a board whose schema this (non-installed) core would have to change first."""


@dataclass(frozen=True)
class Owner:
    """Whether the running core may change the schema of ``home``'s boards, and why."""
    allowed: bool
    home: Path
    installed: Optional[Path]
    running: Path
    installed_id: Optional[str] = None
    running_id: Optional[str] = None
    why: str = ""


def home_of_board(db_path: Any) -> Path:
    """The Hermes home a board file belongs to: ``<home>/kanban.db`` or ``<home>/kanban/boards/<slug>/kanban.db``."""
    path = Path(db_path).expanduser().absolute()
    parent = path.parent
    if parent.parent.name == "boards" and parent.parent.parent.name == "kanban":
        return parent.parent.parent.parent
    return parent


def installed_core(home: Path) -> Optional[Path]:
    """The checkout ``home``'s launcher runs, or None when the home has none (a test or package layout)."""
    candidate = Path(home) / "hermes-agent"
    try:
        if not ((candidate / ".git").exists() or (candidate / "install-stamp.json").exists()):
            return None
        if not (candidate / "hermes_cli").is_dir():
            return None
        return candidate.resolve()
    except OSError:
        return None


def running_core() -> Path:
    """The tree this process's code runs from."""
    from pm.paths import repo_root
    return repo_root().resolve()


def _identity(root: Path) -> Optional[str]:
    try:
        from hermes_cli.boot_bootstrap import current_install_identity
        return current_install_identity(root)
    except Exception:  # health: allow BLE001 -- identity is a best-effort tiebreak; unknown never matches
        return None


_DECISIONS: dict[tuple[str, str], Owner] = {}
_DECISIONS_LOCK = threading.Lock()


def decide(db_path: Any, *, running: Optional[Path] = None) -> Owner:
    """THE predicate: may the running core change the schema of the home that holds ``db_path``?

    Cached per (home, running tree) for the process: an install moves only through ``hermes update``, which
    restarts the processes that run it. The git identity is read only when the two trees differ.
    """
    home = home_of_board(db_path)
    run = Path(running).resolve() if running is not None else running_core()
    key = (str(home), str(run))
    with _DECISIONS_LOCK:
        hit = _DECISIONS.get(key)
    if hit is not None:
        return hit
    installed = installed_core(home)
    if installed is None:
        # Not cached: a few stats, and a home can gain its install later in a long process (an installer).
        return Owner(True, home, None, run, why="this home has no installed core to defer to")
    if installed == run:
        owner = Owner(True, home, installed, run, why="the installed core")
    else:
        iid, rid = _identity(installed), _identity(run)
        if iid and rid and iid == rid:
            owner = Owner(True, home, installed, run, iid, rid, why="the installed core's commit")
        else:
            owner = Owner(False, home, installed, run, iid, rid, why="not this home's installed core")
    with _DECISIONS_LOCK:
        _DECISIONS[key] = owner
    return owner


# --- what this core's schema pass would add ---------------------------------------------------------------

_REFERENCE: Optional[dict[str, frozenset[str]]] = None
_REFERENCE_LOCK = threading.Lock()


def _reference_schema() -> dict[str, frozenset[str]]:
    """Every table and column this core's board init declares, from one in-memory init (once per process)."""
    global _REFERENCE
    with _REFERENCE_LOCK:
        if _REFERENCE is None:
            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_db_connect as kbc
            from hermes_cli.kanban_wake_episode import ensure_schema
            mem = sqlite3.connect(":memory:", isolation_level=None)
            try:
                mem.row_factory = sqlite3.Row
                mem.executescript(kb.SCHEMA_SQL)
                kbc._migrate_add_optional_columns(mem)
                ensure_schema(mem)
                _REFERENCE = _tables(mem)
            finally:
                mem.close()
        return _REFERENCE


def _tables(conn: sqlite3.Connection) -> dict[str, frozenset[str]]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {n: frozenset(r[1] for r in conn.execute(f'PRAGMA table_info("{n}")')) for n in names}


def missing_schema(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Per table, what this core's schema pass would add to ``conn``'s board (``{"<table>": {"*"}}`` for a
    missing table). Empty: the board already holds everything this core declares."""
    live = _tables(conn)
    out: dict[str, set[str]] = {}
    for table, cols in _reference_schema().items():
        have = live.get(table)
        if have is None:
            out[table] = {"*"}
        elif cols - have:
            out[table] = set(cols - have)
    return out


def _short(sha: Optional[str]) -> str:
    return (sha or "unknown")[:12]


def refusal_line(db_path: Any, owner: Owner, missing: dict[str, set[str]]) -> str:
    """The one line a refused write (and the log) shows."""
    named = "; ".join(f"{t}: {', '.join(sorted(c))}" for t, c in sorted(missing.items())) or "a new board"
    return (f"kanban: {db_path} is read-only here: only this home's installed core {owner.installed} "
            f"({_short(owner.installed_id)}) may change its schema, and this process runs {owner.running} "
            f"({_short(owner.running_id)}), which would add ({named}). Test this checkout against a scratch "
            "home instead: HERMES_HOME=$(mktemp -d) ...")


# --- the read-only connection -----------------------------------------------------------------------------

class _ReadOnlyBoard(sqlite3.Connection):
    """A board opened ``mode=ro`` for a non-installed core: SQLite's bare "attempt to write a readonly
    database" (or a statement that needs a table/column this core would have added) becomes
    :class:`BoardSchemaNotOwned` with the refusal line."""

    hermes_schema_refusal = ""
    _SIGNS = ("readonly", "read-only", "has no column named", "no such column", "no such table")

    def _translate(self, call, *args):
        try:
            return call(*args)
        except BoardSchemaNotOwned:
            raise
        except sqlite3.OperationalError as exc:
            if any(sign in str(exc) for sign in self._SIGNS):
                raise BoardSchemaNotOwned(f"{self.hermes_schema_refusal} [sqlite: {exc}]") from exc
            raise

    def execute(self, *args):  # type: ignore[override]
        return self._translate(super().execute, *args)

    def executemany(self, *args):  # type: ignore[override]
        return self._translate(super().executemany, *args)

    def executescript(self, *args):  # type: ignore[override]
        return self._translate(super().executescript, *args)


_WARNED: set[str] = set()
# resolved board path -> refusal line: this process opens it read-only from now on (connect's fast path).
READ_ONLY: dict[str, str] = {}


def open_read_only(path: Path, line: str, *, timeout_s: float, text_factory: Any) -> sqlite3.Connection:
    """``path`` opened read-only; writes raise :class:`BoardSchemaNotOwned` (logged once per board)."""
    from hermes_cli import kanban_lockguard
    resolved = str(Path(path).resolve())
    READ_ONLY[resolved] = line
    if resolved not in _WARNED:
        _WARNED.add(resolved)
        log.warning("%s", line)
    conn = kanban_lockguard.connect(Path(resolved).as_uri() + "?mode=ro", uri=True, isolation_level=None,
                                    timeout=timeout_s, factory=_ReadOnlyBoard)
    conn.hermes_schema_refusal = line
    conn.row_factory = sqlite3.Row
    conn.text_factory = text_factory
    return conn


def check(conn: sqlite3.Connection, path: Path) -> Optional[str]:
    """For :func:`kanban_db_connect.connect`'s first open of ``path`` in this process: ``None`` when this core
    may run the schema pass, ``""`` when it must skip it but the board already holds its schema, else the
    refusal line (open read-only)."""
    owner = decide(path)
    if owner.allowed:
        return None
    missing = missing_schema(conn)
    if not missing:
        return ""
    return refusal_line(path, owner, missing)
