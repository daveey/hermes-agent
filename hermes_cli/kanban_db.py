"""SQLite-backed Kanban board shared across profiles (the cross-profile coordination primitive).

Lives under the shared Hermes root: ``default`` board DB at ``<root>/kanban.db`` (pre-boards
back-compat), other boards at ``<root>/kanban/boards/<slug>/``; a worker on one board never sees
another. Board resolution: ``board=`` arg > ``HERMES_KANBAN_BOARD`` > ``HERMES_KANBAN_DB`` (pins the
file path) > ``<root>/kanban/current`` > ``default`` — but only for unfenced callers; the dispatcher
injects these into workers, and dispatched workers (``HERMES_KANBAN_TASK``), delegated children and
board-enumerating machine flows (gateway notifier/watcher/dispatcher, ``pin_first_board_resolution``)
always resolve through the pin, so workers physically cannot see other boards.
Concurrency: WAL + ``BEGIN IMMEDIATE`` + compare-and-swap on ``tasks.status``/``claim_lock`` —
SQLite serializes writers so one claimer wins, losers see zero rows (no retries, no distributed
locks). Schema: tasks, task_links, task_comments, task_events, task_runs, attachments, notify subs.
"""

from __future__ import annotations

import contextlib
import json

from hermes_cli.kanban_published_worktree import publication_scope
from hermes_cli.kanban_workspace_publication import publication_guard, source_receipt_preflight
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from hermes_cli import kanban_db_sync as _sync, kanban_park_pin as _park_pin, kanban_source_receipt as _sr, kanban_requeue_custody as _requeue_custody, kanban_core_update_requeue as _cu_requeue
from hermes_cli.kanban_db_values import _first_line, _opt_int, _json_or_null
from hermes_cli.kanban_db_event import Event  # public kb.Event; helpers import lazily: no cycle
from hermes_cli.kanban_worker_placement import resolved_db_placement, validate_creation
from hermes_cli import kanban_dup_detector as _dup_detector, kanban_placement_forward as _placement_fwd  # no cycle
from hermes_cli import kanban_run_end_lease as _run_end_lease  # stdlib-only at import: no cycle
from hermes_cli import kanban_containment_connections as _containment, kanban_spend_claim as _spend_claim
from hermes_cli.kanban_host_identity import is_local_claim as _claim_is_local
from hermes_cli.kanban_db_dispatch_queries import DISPATCH_ORDER  # stdlib-only at import: no cycle
from hermes_cli.kanban_terminal_guard import unblock_refusal  # stdlib-only at import: no cycle; re-exported for plugins
from hermes_cli.kanban_admission import admission_hold_set, claim_transaction, claim_and_open_run, hub_cpu_advisory
from hermes_cli.kanban_workflow import DEFAULT_STATUSES as VALID_STATUSES
from toolsets import get_toolset_names
from utils import unlink_files_older_than

_log = logging.getLogger(__name__)


# --- Shared micro-helpers (row access, JSON, env, git) ---

from hermes_cli.kanban_db_values import _lossy_text
from hermes_cli.kanban_admission_class import validate as validate_admission_class  # module scope: not in locals()


def _row_get(row: Any, col: str, default: Any = None) -> Any:
    """``row[col]`` tolerant of the column being absent from the SELECT / schema."""
    if row is None or col not in row.keys():
        return default
    return row[col]


def _json_or(value: Any, default: Any = None) -> Any:
    """Decode a JSON text column; any decode failure or empty value yields ``default``."""
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dict(value: Any) -> dict:
    """Decode a JSON text column that must be an object; anything else yields ``{}``."""
    parsed = _json_or(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """Integer env override: absent/empty/non-integer/below ``minimum`` falls back to ``default``."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return default
        if parsed >= minimum:
            return parsed
    return default


def _git_out(cwd: Path, *args: str, timeout: int = 30) -> Optional[str]:
    """Run ``git -C cwd args`` and return stripped stdout, or ``None`` on any failure / empty output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


# --- Constants ---

VALID_INITIAL_STATUSES = {"running", "blocked"}

# Typed block reasons (routing in kanban_block_routing); None = legacy untyped.
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "assignment", "transient", "time"}

# Same-reason block -> unblock -> re-block cycles before routing to ``triage``.
# Counts unblock recurrences, NOT dispatcher failures (``DEFAULT_FAILURE_LIMIT``).
BLOCK_RECURRENCE_LIMIT = 2
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}


def normalize_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """``VALID_REASONING_EFFORTS`` or ``"none"`` (thinking off), case-insensitive;
    empty/None = inherit the profile's own effort (NULL). Anything else raises —
    a typo'd level must not quietly hand the task back to the profile default."""
    from hermes_constants import VALID_REASONING_EFFORTS

    value = str(effort or "").strip().lower()
    if not value:
        return None
    if value == "none" or value in VALID_REASONING_EFFORTS:
        return value
    allowed = ", ".join(("none", *VALID_REASONING_EFFORTS))
    raise ValueError(f"reasoning_effort must be one of {allowed}, got {effort!r}")


KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"
KANBAN_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # one cap for dashboard, tools and CLI


def _assert_not_delegated_child_mutation(path: str | Path | None = None) -> None:
    """Reject Kanban mutations from fenced contexts (delegate_task children, worker-tool
    descendants); see :mod:`hermes_cli.kanban_db_fence`. Every ``write_txn`` user and
    board-metadata mutator calls this, so the invariant fails closed below the UX guards."""
    from hermes_cli.kanban_db_fence import assert_not_fenced

    assert_not_fenced(kanban_home() if path is None else path)


def _fire_kanban_lifecycle_hook(event: str, task_id: str, **fields: Any) -> None:
    """Best-effort lifecycle hook. Call AFTER the write txn commits (plugins never
    run under the SQLite write lock, always see durable state); failures are
    swallowed so an observer can never break a transition."""
    try:
        from hermes_cli.lifecycle import invoke_hook

        invoke_hook(event, task_id=task_id, profile_name=_hook_profile_name(), **fields)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban lifecycle hook %s failed: %s", event, exc)


def _fire_task_hook(event: str, task: Optional[Task], task_id: str, run_id: Optional[int], **fields: Any) -> None:
    """Lifecycle hook for a task transition; ``assignee`` from the (possibly missing) row."""
    _fire_kanban_lifecycle_hook(
        event, task_id, board=get_current_board(),
        assignee=task.assignee if task else None, run_id=run_id, **fields,
    )


def _hook_profile_name() -> str:
    """Active profile for hook payloads; ``"default"`` when it cannot be resolved."""
    from hermes_cli.profiles import get_active_profile_name

    try:
        return get_active_profile_name()
    except Exception:
        return "default"


def _kanban_observer_consumed(event: str) -> bool:
    """Hot-path short-circuit: skip payload assembly when nothing subscribes.
    Inspection failure counts as unconsumed (dropping an observer is always safe)."""
    try:
        from hermes_cli.lifecycle import has_hook

        return has_hook(event)
    except Exception:  # pragma: no cover - defensive
        return False


def _fire_worker_spawned_hook(
    conn: sqlite3.Connection, task: Task, workspace_path: str, pid: Optional[int], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_worker_spawned`` AFTER the PID is durably persisted; best-effort."""
    if not _kanban_observer_consumed("on_kanban_worker_spawned"):
        return
    try:
        _fire_kanban_lifecycle_hook(
            "on_kanban_worker_spawned", task.id, board=board or get_current_board(),
            assignee=task.assignee, run_id=_current_run_id(conn, task.id),
            worker_pid=int(pid) if pid else None, workspace_path=str(workspace_path),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban worker spawned hook failed: %s", exc)


def notify_task_updated(
    conn: sqlite3.Connection, task_id: str, changed_fields: Iterable[str], *,
    board: Optional[str] = None,
) -> None:
    """``on_kanban_task_updated`` AFTER a non-lifecycle task mutation commits
    (also for direct-SQL surfaces like dashboard field editors).
    ``changed_fields`` carries field NAMES only, never values."""
    if not _kanban_observer_consumed("on_kanban_task_updated"):
        return
    try:
        row = conn.execute(
            "SELECT assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        _fire_kanban_lifecycle_hook(
            "on_kanban_task_updated", task_id, board=board or get_current_board(),
            assignee=row["assignee"] if row else None,
            run_id=row["current_run_id"] if row else None, changed_fields=list(changed_fields),
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban task updated hook failed: %s", exc)


# DispatchResult counters whose non-zero value means the tick did something.
_TICK_ACTIVITY_FIELDS = (
    "spawned", "reclaimed", "promoted", "reconciled_orphans", "reaped_terminal_workers", "stopped_run_scopes",
    "crashed", "stale", "timed_out", "auto_blocked", "rate_limited", "auto_assigned_default",
    "respawn_guarded", "skipped_per_profile_capped", "skipped_unassigned",
    "skipped_nonspawnable", "woken_continuations",
)


def _fire_dispatch_tick_hook(
    result: DispatchResult, *, board: Optional[str] = None, dry_run: bool = False,
) -> None:
    """``on_kanban_dispatch_tick`` — strictly AFTER ``_dispatch_tick_lock`` is
    released so a slow subscriber cannot stall a sibling dispatcher.

    Re-port of PR #56066 per the #64231 batch disposition: renamed to the taxonomy form and called by
    ``dispatch_once`` strictly AFTER ``_dispatch_tick_lock`` has been released — the original fired inside
    the lock, so a slow subscriber could extend the single-writer critical section and stall a sibling
    dispatcher's tick. Observer-only and fully best-effort: any subscriber failure is swallowed.
    """
    if not _kanban_observer_consumed("on_kanban_dispatch_tick"):
        return
    try:
        from hermes_cli.lifecycle import invoke_hook

        profile_name = _hook_profile_name()
        if board is None:
            try:
                board = get_current_board()
            except Exception:
                board = None
        outcome = "ok"
        if result.skipped_locked:
            outcome = "skipped_locked"
        elif not any(getattr(result, f) for f in _TICK_ACTIVITY_FIELDS):
            outcome = "idle"
        invoke_hook(
            "on_kanban_dispatch_tick", board=board, profile_name=profile_name,
            dry_run=bool(dry_run), outcome=outcome, result=result,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("kanban dispatch tick hook failed: %s", exc)


# Claim window before the next tick reclaims a running task; long workers
# ``heartbeat_claim`` or raise it via HERMES_KANBAN_CLAIM_TTL_SECONDS.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# A live PID with a heartbeat older than this is wedged and reclaimed anyway
# (``_touch_activity`` keeps genuinely active workers fresh).
# If a worker's PID is still alive but its ``last_heartbeat_at`` is older than this when
# ``release_stale_claims`` runs, treat the worker as wedged and reclaim regardless of PID liveness (#29747
# gap 3). This catches the logic-loop case where the process is technically running but not making
# observable progress. ``_touch_activity`` bridges chunk-level liveness into ``last_heartbeat_at`` via
# #31752, so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Grace when a host-local worker survived termination (e.g. parked in D state
# under memory.high, SIGKILL pending): releasing now would spawn a duplicate.
RECLAIM_DEFER_GRACE_SECONDS = 120


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Explicit ``ttl_seconds`` > ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` > default."""
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    return _env_int("HERMES_KANBAN_CLAIM_TTL_SECONDS", DEFAULT_CLAIM_TTL_SECONDS, minimum=1)


# ``detect_crashed_workers`` skips ``_pid_alive`` this long after start: the
# fork -> /proc window can report a fresh worker dead.
DEFAULT_CRASH_GRACE_SECONDS = 30

# Worker exit "provider rate-limited": released WITHOUT counting a failure (the
# breaker must never trip on a throttle). 75 == BSD EX_TEMPFAIL.
KANBAN_RATE_LIMIT_EXIT_CODE = 75

# Worker exit "provider rejected the configuration": credential revoked (401/403), model gone
# (404), TLS chain broken — a retry cannot fix it, so the dispatcher parks the card blocked on
# the FIRST occurrence instead of spending ``failure_limit`` identical spawns. 78 == BSD EX_CONFIG.
KANBAN_TERMINAL_PROVIDER_EXIT_CODE = 78


def _resolve_crash_grace_seconds() -> int:
    """``HERMES_KANBAN_CRASH_GRACE_SECONDS`` (0 = immediate, for tests) else default."""
    return _env_int("HERMES_KANBAN_CRASH_GRACE_SECONDS", DEFAULT_CRASH_GRACE_SECONDS)


def _resolve_rate_limit_cooldown_seconds() -> int:
    """``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` (0 = next tick, for tests) else default."""
    return _env_int("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS)


# --- Paths ---

DEFAULT_BOARD = "default"
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override", default=None,
)
# Machine flows that enumerate boards (gateway notifier / watcher / dispatcher
# ticks) resolve board paths env-pin-first — see pin_first_board_resolution().
_PIN_FIRST_BOARD_RESOLUTION: ContextVar[bool] = ContextVar(
    "hermes_kanban_pin_first_board_resolution", default=False,
)


@contextlib.contextmanager
def pin_first_board_resolution():
    """Resolve board paths env-pin-first for machine flows that enumerate boards.

    The gateway notifier, per-subscription cursor writes and the embedded
    dispatcher poll every board slug from ``list_boards()`` — the slug is not
    caller intent, it is an iteration variable. On a box whose environment pins
    ``HERMES_KANBAN_DB`` (the dispatcher default) every slug must map to that
    one pinned file: the notifier dedupes resolved DB paths, and per-slug
    paths read empty boards nobody writes, silently killing wake
    notifications. Explicit cross-board intent (CLI ``--board``, a model
    tool's ``board=``) is a USER property and must never run inside this
    context; with no pin set this context changes nothing.
    """
    token = _PIN_FIRST_BOARD_RESOLUTION.set(True)
    try:
        yield
    finally:
        _PIN_FIRST_BOARD_RESOLUTION.reset(token)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)


# Slug = directory name: strict enough to stop traversal / separators, loose
# enough for kebab-case. Display names (spaces, emoji) live in board.json.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    s = str(slug).strip().lower() if slug is not None else ""
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def _slug_or_default(board: Optional[str]) -> str:
    return _normalize_board_slug(board) or DEFAULT_BOARD


def _require_slug(slug: str) -> str:
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    return normed


def kanban_home() -> Path:
    """``HERMES_KANBAN_HOME`` else ``get_default_hermes_root()``. Shared across
    profiles BY DESIGN: resolving through the active profile's HERMES_HOME would
    fork the board per profile and break the dispatcher/worker handoff."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """``<root>/kanban/boards`` — parent of the *additional* named boards.
    ``default`` is deliberately not here (its DB stays at ``<root>/kanban.db``)."""
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """``<root>/kanban/current`` — one-line slug written by ``boards switch``; absent = ``default``."""
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Active slug: context override -> ``HERMES_KANBAN_BOARD`` -> ``<root>/kanban/current``
    (only while that board exists) -> ``DEFAULT_BOARD``. A malformed/stale slug
    falls through — the dispatcher must never crash on a hand-edited file."""
    def _existing(candidate: str) -> Optional[str]:
        if not candidate:
            return None
        try:
            normed = _normalize_board_slug(candidate)
        except ValueError:
            return None
        if not (normed and board_exists(normed)):
            return None
        # An archived board leaves a tombstone board.json; a stale `current`
        # file or HERMES_KANBAN_BOARD pointing at one falls through to
        # default rather than resurrecting it (#43243).
        if read_board_metadata(normed).get("archived"):
            return None
        return normed

    for candidate in (
        (_CURRENT_BOARD_OVERRIDE.get() or "").strip(),
        os.environ.get("HERMES_KANBAN_BOARD", "").strip(),
    ):
        found = _existing(candidate)
        if found:
            return found
    try:
        f = current_board_path()
        if f.exists():
            # utf-8-sig read fix (ours): tolerate BOM-persisted current-board files.
            val = f.read_text(encoding="utf-8-sig").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board; returns the file written. Does NOT
    check the board exists — callers do (so ``boards switch <typo>`` errors)."""
    _assert_not_delegated_child_mutation()
    normed = _require_slug(slug)
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    _assert_not_delegated_child_mutation()
    with contextlib.suppress(FileNotFoundError):
        current_board_path().unlink()


def board_dir(board: Optional[str] = None) -> Path:
    """``<root>/kanban/boards/<slug>/``. For ``default`` this holds metadata
    only (board.json, workspaces/, logs/) — its DB stays at ``<root>/kanban.db``
    for back-compat (:func:`kanban_db_path`).
    """
    return boards_root() / _slug_or_default(board)


def board_exists(board: Optional[str] = None) -> bool:
    """Board has a ``board.json`` on disk; ``default`` always exists."""
    slug = _slug_or_default(board)
    if slug == DEFAULT_BOARD:
        return True
    return _dir_holds_board(board_dir(slug))




def _explicit_board_slug(board: Optional[str]) -> Optional[str]:
    """Explicit caller intent: a direct ``board=`` argument, else the scoped
    ``--board`` context (CLI ``hermes kanban --board``, dashboard plugin_api);
    ``None`` when the caller expressed neither."""
    if board is not None:
        return _normalize_board_slug(board)
    # A caller-scoped board (CLI `hermes kanban --board B ...`, dashboard
    # plugin_api) is explicit intent just like a direct board= argument —
    # without this a worker-pinned HERMES_KANBAN_DB silently outranks --board
    # (os-reviewer P1 on PR#107195 / t_11c4afd8).
    ctx = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if ctx:
        try:
            return _normalize_board_slug(ctx)
        except ValueError:
            return None
    return None


def _explicit_board_intent_pinned() -> bool:
    """Whether an explicit ``board=`` (or scoped ``--board``) must still resolve
    through the ``HERMES_KANBAN_DB``-style env pins instead of its own board dir.

    True for machine flows wrapped in :func:`pin_first_board_resolution` and
    for every execution the dispatcher fences: its own workers (they carry
    ``HERMES_KANBAN_TASK``) and delegated children / descendants (the
    ``HERMES_DELEGATED_CHILD_CONTEXT`` marker). The pins ARE the "workers
    physically cannot see other boards" isolation (5ec6baa), and
    ``agent.delegation_context.kanban_path_is_fenced`` checks the pinned path /
    fenced root — an explicit board that resolved elsewhere would also escape
    that fence."""
    if _PIN_FIRST_BOARD_RESOLUTION.get():
        return True
    from agent.delegation_context import explicit_board_intent_is_pinned
    return explicit_board_intent_is_pinned()


def _board_path(
    env_var: Optional[str], board: Optional[str], default_parts: tuple[str, ...], leaf: str,
) -> Path:
    """Shared resolver. An explicit ``board=`` argument — or the scoped
    ``--board`` context (:func:`scoped_current_board`) — outranks the ``env_var``
    pin ONLY where no fence applies (see :func:`_explicit_board_intent_pinned`):
    user-facing cross-board intent (CLI ``--board``, a model tool's ``board=``)
    is honored, but machine flows that enumerate boards (gateway notifier /
    watcher / dispatcher ticks) and dispatched or delegated workers keep
    resolving through the pin. Without explicit intent the ``env_var`` override
    pins the file, else legacy ``<root>/<default_parts>`` for the ``default``
    board, else ``board_dir(slug)/leaf``."""
    pin = os.environ.get(env_var, "").strip() if env_var else ""
    slug = _explicit_board_slug(board)
    if pin and (slug is None or (_explicit_board_intent_pinned()
                                 and not _descendant_may_follow_explicit_board(slug))):
        return Path(pin).expanduser()
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home().joinpath(*default_parts)
    return board_dir(slug) / leaf


def _descendant_may_follow_explicit_board(slug: str) -> bool:
    """A worker-terminal descendant's explicit board wins over the pin when the board's own DB
    stays inside the write fence (:func:`agent.delegation_context.explicit_board_resolves_inside_fence`).
    Machine flows (:func:`pin_first_board_resolution`) never follow it. [daveey]"""
    if _PIN_FIRST_BOARD_RESOLUTION.get():
        return False
    from agent.delegation_context import explicit_board_resolves_inside_fence
    return explicit_board_resolves_inside_fence(board_own_db_path(slug))


def board_own_db_path(slug: Optional[str]) -> Path:
    """The board's own ``kanban.db``, ignoring every env pin: ``<root>/kanban.db`` for
    ``default``, else ``board_dir(slug)/kanban.db``. For read-only listings that must describe
    each board (``boards list`` counts), and the exact-grant owner-wake tool resolver. [daveey]"""
    s = _slug_or_default(slug)
    if s == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(s) / "kanban.db"


def kanban_db_path(board: Optional[str] = None) -> Path:
    """``kanban.db`` path: ``HERMES_KANBAN_DB`` pins it (injected into workers);
    ``default`` -> ``<root>/kanban.db`` (back-compat), else the board dir."""
    return _board_path("HERMES_KANBAN_DB", board, ("kanban.db",), "kanban.db")


def workspaces_root(board: Optional[str] = None) -> Path:
    """Per-board scratch workspace root (``HERMES_KANBAN_WORKSPACES_ROOT`` wins);
    ``default`` keeps the legacy ``<root>/kanban/workspaces/``."""
    return _board_path("HERMES_KANBAN_WORKSPACES_ROOT", board, ("kanban", "workspaces"), "workspaces")


def attachments_root(board: Optional[str] = None) -> Path:
    """Per-board attachments root (``HERMES_KANBAN_ATTACHMENTS_ROOT`` wins). Workers
    read attachments by absolute path, so remote terminal backends must mount it."""
    return _board_path("HERMES_KANBAN_ATTACHMENTS_ROOT", board, ("kanban", "attachments"), "attachments")


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Per-board worker log dir (logs follow the board so ``hermes kanban log``
    is unambiguous when two boards share a task id)."""
    return _board_path(None, board, ("kanban", "logs"), "logs")


# --- Data classes ---

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: Optional[int]  # None only for an unparseable stamp
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Column semantics: see SCHEMA_SQL.
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    skills: Optional[list] = None            # None = defaults only; [] = explicitly none
    requires: Optional[list] = None          # Configured assignment capabilities
    model_override: Optional[str] = None
    provider_override: Optional[str] = None  # provider ``model_override`` belongs to
    model_pin_mandatory: Optional[int] = None  # 1 = never rerouted around a quota wall
    reasoning_effort: Optional[str] = None   # VALID_REASONING_EFFORTS | "none"; NULL = profile's
    # Breaker trip count; None -> ``kanban.failure_limit`` -> DEFAULT_FAILURE_LIMIT.
    max_retries: Optional[int] = None
    # ``/goal``-style loop: a judge re-checks each turn IN THE SAME SESSION until
    # done / budget exhausted (-> kanban_block); ``goal_max_turns`` None -> goals default.
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    review_required: bool = False
    session_id: Optional[str] = None         # originating HERMES_SESSION_ID; NULL from CLI/dashboard
    # VALID_BLOCK_KINDS or None (legacy); kept across unblock so a same-kind re-block reads as a loop.
    block_kind: Optional[str] = None
    human_request: Optional[dict] = None
    containment_episode: Optional[str] = None  # Portable hold marker; never release authority.
    block_recurrences: int = 0               # unblock-loop counter, see BLOCK_RECURRENCE_LIMIT
    completion_contract: Optional[str] = None
    placement_policy: Optional[str] = None
    admission_class: str = "normal"
    admission_frees: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        g = lambda col, default=None: _lossy_text(_row_get(row, col, default))
        parsed = _json_or(g("skills"))
        skills_value = [str(s) for s in parsed if s] if isinstance(parsed, list) else None
        return cls(
            **{col: _lossy_text(row[col]) for col in _TASK_REQUIRED_COLUMNS},
            **{col: _to_epoch(g(col)) for col in _TASK_EPOCH_COLUMNS},  # outside writers may store text: epoch or None
            **{col: g(col) for col in _TASK_OPTIONAL_COLUMNS},
            **{col: g(col) or None for col in _TASK_EMPTY_IS_NULL_COLUMNS},
            # Pre-migration fallbacks (spawn_failures / last_spawn_error) are only
            # reachable on a DB never opened since the rename migration landed.
            consecutive_failures=g("consecutive_failures", g("spawn_failures", 0)),
            last_failure_error=g("last_failure_error", g("last_spawn_error")),
            skills=skills_value,
            human_request=_json_or(g("human_request")),
            requires=_json_or(g("requires")) or [],
            goal_mode=bool(g("goal_mode")),
            review_required=bool(g("review_required", 0)),
            block_recurrences=int(g("block_recurrences") or 0),
            admission_class=g("admission_class", "normal"),
            admission_frees=g("admission_frees"),
        )


# Private aliases remain available to direct callers of the compatibility facade.
from hermes_cli.kanban_task_fields import EMPTY_IS_NULL as _TASK_EMPTY_IS_NULL_COLUMNS
from hermes_cli.kanban_task_fields import EPOCH as _TASK_EPOCH_COLUMNS
from hermes_cli.kanban_task_fields import OPTIONAL as _TASK_OPTIONAL_COLUMNS
from hermes_cli.kanban_task_fields import REQUIRED as _TASK_REQUIRED_COLUMNS


@dataclass
class Run:
    """One attempt at a task (``task_runs`` row): opened on claim, closed on
    complete/block/crash/timeout/reclaim; carries the handoff summary."""

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Run:
        return cls(
            **{
                col: _lossy_text(row[col]) for col in (
                    "task_id", "profile", "step_key", "status", "claim_lock",
                    "worker_pid", "max_runtime_seconds", "outcome", "summary", "error",
                )
            },
            **{col: _to_epoch(row[col]) for col in ("claim_expires", "last_heartbeat_at", "ended_at")},
            id=int(row["id"]), started_at=_to_epoch(row["started_at"]) or 0,
            metadata=_json_or(_lossy_text(row["metadata"])),
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Comment:
        return cls(
            id=r["id"], task_id=r["task_id"], author=_lossy_text(r["author"]),
            body=_lossy_text(r["body"]), created_at=_to_epoch(r["created_at"]) or 0,
        )


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
    created_at: int

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> Attachment:
        return cls(
            id=r["id"], task_id=r["task_id"], filename=r["filename"],
            stored_path=r["stored_path"], content_type=r["content_type"],
            size=r["size"] or 0, uploaded_by=r["uploaded_by"], created_at=_to_epoch(r["created_at"]) or 0,
        )


# --- Schema ---

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,
    -- the task's worktree is anchored under the project's primary repo with a
    -- deterministic branch name instead of a random wt/<task-id> fallback.
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Restart-stable fingerprint of worker_pid ("<boot/instantiation epoch>|<start time>",
    -- kanban_db_dispatch._process_fingerprint) recorded at spawn: liveness and kills require pid
    -- AND fingerprint to agree, so a PID recycled after a reboot is never read as our worker or
    -- signalled. NULL = legacy row (pre-fingerprint spawn); 'unverified' = capture failed at
    -- spawn (held while live, never signalled). Column keeps its INTEGER affinity for the
    -- start-time-only integer values older rows carry.
    worker_started_at    INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Passed to the worker via `--skills`. NULL or empty array = no extras.
    skills               TEXT,
    requires             TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Provider the model override belongs to. When set (alongside
    -- model_override), the dispatcher passes --provider <name> so the
    -- worker resolves the model against the right backend instead of the
    -- profile's configured provider. NULL = profile provider.
    provider_override    TEXT,
    -- Per-task reasoning effort for the worker (minimal|low|medium|high|
    -- xhigh|max|ultra, or 'none' for thinking off). When set, the dispatcher
    -- passes --reasoning <level> so the worker runs at that depth regardless
    -- of the profile's agent.reasoning_effort. NULL = profile setting.
    reasoning_effort     TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- Goal loop: an auxiliary judge checks each turn against the card title/body,
    -- continuing in the SAME session until done or ``goal_max_turns`` is exhausted.
    -- NULL/0 = classic single-shot worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    review_required      INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var, and for an id with no ``sessions`` row in this
    -- profile's state.db (kanban_create verifies before stamping). Indexed
    -- so per-session list queries stay cheap on larger boards.
    session_id           TEXT,
    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or
    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never
    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go
    -- to ``blocked`` for a human. Preserved across unblock so a re-block for
    -- the SAME kind can be recognised as a loop.
    block_kind           TEXT,
    human_request        TEXT,
    containment_episode  TEXT,
    -- Unblock-loop counter. Incremented each time a task is re-blocked for the
    -- same truly-blocked reason after having been unblocked. When it reaches
    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of
    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a
    -- successful completion — NOT on unblock (resetting on unblock is exactly
    -- the amnesia that let the loop run unbounded).
    block_recurrences    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    origin_gateway TEXT
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    -- Spawn-time start fingerprint of worker_pid (see tasks.worker_started_at). Retained with
    -- worker_pid after the run ends so a worker that outlives its terminal transition can
    -- still be found and reaped; NULL = legacy row, never signalled.
    worker_started_at   INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | continued | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    inflight_from INTEGER,
    inflight_pid  INTEGER,
    inflight_start INTEGER, inflight_at INTEGER, created_event_id INTEGER,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
-- Condition-wake probes use one rewritten row per card (``kanban_wake_check``). [daveey]
CREATE TABLE IF NOT EXISTS task_wake_checks (
    task_id   TEXT PRIMARY KEY,
    cmd       TEXT NOT NULL,
    every     INTEGER NOT NULL,
    next_at   INTEGER NOT NULL,
    last_at   INTEGER,
    last_rc   INTEGER,
    last_out  TEXT,
    fails     INTEGER NOT NULL DEFAULT 0, first_failure_at INTEGER, confirm_fails INTEGER NOT NULL DEFAULT 0,
    generation TEXT, purpose TEXT NOT NULL DEFAULT 'wake'  -- 'wake' (park) | 'ask_resolved' (kanban_ask_resolved)
);
CREATE INDEX IF NOT EXISTS idx_wake_checks_next_at  ON task_wake_checks(next_at);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_kind_task_id   ON task_events(kind, task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_park_history ON task_events(id) WHERE kind IN ('scheduled','continued') AND typeof(kind)='text';
CREATE INDEX IF NOT EXISTS idx_events_wait_end ON task_events(task_id,id) WHERE kind IN ('condition_met','continuation_woken','promoted','promoted_manual','unblocked','claimed','completed','archived') AND typeof(kind)='text';
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
"""

# --- ID generation ---

def _new_task_id() -> str:
    """``t_`` + 4 hex bytes (collision ~1e-3 at 100k tasks; 2 bytes would hit 50%
    by 10k). Idempotency belongs to ``idempotency_key``, not id uniqueness."""
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    from hermes_cli.kanban_host_identity import claim_prefix
    return f"{claim_prefix()}{os.getpid()}"


def _host_prefix() -> str:
    """Stable local claim prefix, shared across dispatcher restarts."""
    return f"{_claimer_id().split(':', 1)[0]}:"


# --- Task creation / mutation ---

def _validate_model_override(model: Optional[str], provider: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Validate model-neutral class routing at the common mutation boundary."""
    from hermes_cli.kanban_model_neutral import validate_selector
    return validate_selector(model, provider)


def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def _resolve_project_link(
    conn: sqlite3.Connection, project_id: Optional[str], project_source_task_id: Optional[str],
    workspace_kind: str, workspace_path: Optional[str],
) -> tuple[Optional[str], Any, Optional[str], str]:
    """``(project_id, project_obj, project_repo, workspace_kind)`` for ``create_task``.

    A project-linked task is anchored to the project's primary repo as a
    worktree with a deterministic branch (slug + task id). Projects live in the
    creator's per-profile projects.db, but the stored repo path is absolute so
    the cross-profile dispatcher needs no projects.db access. ``project_repo``
    is set when the worktree path must still be derived from the new task id.
    """
    project_id = (str(project_id).strip() or None) if project_id is not None else None
    if not project_id:
        return None, None, None, workspace_kind
    from hermes_cli import projects_db as _pdb

    project_repo: Optional[str] = None
    try:
        with _pdb.connect_closing() as _pconn:
            project_obj = _pdb.get_project(_pconn, project_id)
    except Exception:
        project_obj = None
    if project_obj is None and project_source_task_id:
        project_obj, project_repo = _project_from_source_task(
            conn, _pdb, project_id, str(project_source_task_id),
        )
        if project_obj is not None and workspace_kind == "scratch":
            workspace_kind = "worktree"
    if project_obj is None:
        # Unresolvable id/slug: drop the link (never a dangling reference,
        # never a crash) and create an ordinary scratch task.
        return None, None, None, workspace_kind
    # Canonicalise (a slug may have been passed) and anchor the worktree
    # under the project's primary repo.
    if workspace_kind == "scratch" and project_obj.primary_path:
        workspace_kind = "worktree"
    if workspace_kind == "worktree" and workspace_path is None and project_obj.primary_path:
        # Concrete path is deferred to the insert loop: a fresh
        # ``<repo>/.worktrees/<task-id>`` keyed on the new task id.
        project_repo = str(project_obj.primary_path)
    return project_obj.id, project_obj, project_repo, workspace_kind


def _project_from_source_task(
    conn: sqlite3.Connection, _pdb: Any, project_id: str, source_task_id: str,
) -> tuple[Any, Optional[str]]:
    """Recover a Project (and its repo) from a canonical project-linked
    worktree task on this board. Worker profiles have their own projects.db
    while the Kanban DB is shared, so this carries the repo + branch
    convention forward without opening the creator's store and without
    reusing the source task's literal worktree path. ``(None, None)`` when
    the source task is not a ``<repo>/.worktrees/<id>`` project worktree."""
    source_task = get_task(conn, source_task_id)
    if not (
        source_task is not None
        and source_task.project_id == project_id
        and source_task.workspace_kind == "worktree"
        and source_task.workspace_path
    ):
        return None, None
    source_path = Path(source_task.workspace_path)
    if not (
        source_path.is_absolute()
        and source_path.name == source_task.id
        and source_path.parent.name == ".worktrees"
    ):
        return None, None
    project_slug = None
    if source_task.branch_name:
        prefix, separator, leaf = source_task.branch_name.partition("/")
        if separator and (leaf == source_task.id or leaf.startswith(f"{source_task.id}-")):
            with contextlib.suppress(ValueError):
                project_slug = _pdb.normalize_slug(prefix)
    if project_slug is None:
        with contextlib.suppress(ValueError):
            project_slug = _pdb.normalize_slug(project_id)
    if not project_slug:
        return None, None
    project_repo = str(source_path.parent.parent)
    project_obj = _pdb.Project(
        id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo,
    )
    return project_obj, project_repo


def _normalize_task_skills(skills: Optional[Iterable[str]]) -> Optional[list[str]]:
    """Strip/dedupe a skills list. Commas are refused (a comma-joined string must
    not land in one argv slot); toolset names are rejected all at once because
    agents that confuse the two usually pass several."""
    if skills is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    toolset_typos: list[str] = []
    for s in skills:
        if not s:
            continue
        name = str(s).strip()
        if not name:
            continue
        if "," in name:
            raise ValueError(
                f"skill name cannot contain comma: {name!r} "
                f"(pass a list of separate names instead of a comma-joined string)"
            )
        if name.casefold() in KNOWN_TOOLSET_NAMES:
            toolset_typos.append(name)
            continue
        if name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    if toolset_typos:
        quoted = ", ".join(repr(n) for n in toolset_typos)
        noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
        raise ValueError(
            f"{quoted} {noun}, not skill name(s). "
            "Put toolsets in the assignee profile's `toolsets:` config "
            "instead of per-task skills. Skills are named skill bundles "
            "(e.g. `blogwatcher`, `github-code-review`); toolsets are runtime "
            "capabilities (e.g. `web`, `browser`, `terminal`)."
        )
    return cleaned


from hermes_cli.kanban_db_creation import default_ready_assignee as _default_ready_assignee
from hermes_cli.kanban_selfwork import admitted_contract
from hermes_cli import kanban_origin_admission as native_admission

@publication_scope
def create_task(
    conn: sqlite3.Connection, *, title: str, body: Optional[str] = None,
    assignee: Optional[str] = None, created_by: Optional[str] = None,
    workspace_kind: Optional[str] = None, workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None, tenant: Optional[str] = None, priority: int = 0,
    parents: Iterable[str] = (), triage: bool = False, idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None, skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None, model_override: Optional[str] = None,
    provider_override: Optional[str] = None, reasoning_effort: Optional[str] = None, pin_mandatory: bool = False,
    goal_mode: bool = False, goal_max_turns: Optional[int] = None, initial_status: str = "running",
    session_id: Optional[str] = None, board: Optional[str] = None, project_id: Optional[str] = None,
    project_source_task_id: Optional[str] = None, creator_task_id: Optional[str] = None,
    completion_contract: Optional[str] = None, review_required: bool = False,
    validate_workspace: bool = False, validate_skills: bool = False,
    human_request: Optional[dict] = None,
    origin_intent: Optional[dict] = None, chat_origin: Optional[str] = None, native_origin: Optional[object] = None,
    requires: Optional[list[str]] = None,
    lint_blocked: bool = True, runs_on: Optional[Iterable[str]] = None, require_declaration: bool = False,
    placement_policy: Optional[str] = None,
    placement_checked: bool = False,
    insert_guard: Optional[Callable[[sqlite3.Connection], None]] = None, placement_audit: Optional[dict] = None,
    admission_class: str = "normal", admission_frees: Optional[str] = None,
    self_work: Optional[dict] = None, creation_receipt: Optional[dict] = None,
    require_scope_authority: bool = False, owner_producer: Optional[str] = None, settle_owner_chat: bool = True,
) -> str:
    """Create a task (optionally under ``parents``); returns its id.

    ``lint_blocked=False`` is for internal trackers parked blocked without a human ask
    (swarm root); everything else must pass ``human_request`` with ``initial_status='blocked'``.
    ``origin_intent`` atomically persists the task and receipt intent, not origin readback.
    ``validate_workspace=True`` (agent/CLI create surfaces) rejects a non-project ``worktree`` workspace
    that could never resolve at spawn (``WorktreeWorkspaceConfigError``); ``validate_skills=True`` rejects
    a pinned skill the assignee cannot load (``UnknownSkillError``). [daveey] ``runs_on``: placement labels.
    ``insert_guard``: see ``kanban_db_creation.run_insert_guard`` (the overlap re-check under the write lock).

    Status: ``ready`` unless a parent is not ``done`` (``todo``); ``triage=True``
    forces ``triage``; ``initial_status="blocked"`` parks it for human ops.
    ``idempotency_key``: an existing non-archived task with the key is returned
    instead of a duplicate. ``max_runtime_seconds``: cap before the dispatcher
    SIGTERMs and re-queues. ``model_override``/``provider_override`` pin the
    worker model (provider requires model); ``reasoning_effort`` is independent.
    ``creator_task_id``: inherit durable session/subscriptions independently of
    dependency edges; an explicit ``session_id`` still wins.
    ``project_source_task_id``: cross-profile fallback when ``project_id`` is not
    in the active profile's projects.db — see ``_resolve_project_link``.
    ``workspace_kind=None`` (omitted) inherits a project-scoped board's project;
    an explicit ``"scratch"`` or ``project_id=""`` is a request for no project.
    """
    from hermes_cli.kanban_model_neutral import validate_selector, validate_text
    validate_selector(model_override, provider_override, mandatory=pin_mandatory)
    validate_text(title, body)
    native_admission.check_entry(conn, origin_intent)
    self_work = admitted_contract(conn, self_work)
    validate_admission_class(admission_class, admission_frees)
    validate_creation(placement_policy, runs_on=runs_on, workspace_kind=workspace_kind,
                      workspace_path=workspace_path, project=project_id,
                      origin_intent=origin_intent, board=board)
    if origin_intent is not None:
        from hermes_cli.kanban_lane_classifier import create_origin

        return create_origin(conn, origin_intent, locals())

    from hermes_cli.kanban_lanes import initial_state as lane_state
    from hermes_cli.kanban_creator_chats import initialize_ownership
    from hermes_cli.kanban_pr_acceptance import validate_contract
    from hermes_cli.kanban_human_request import validate_initial_request

    human_request = validate_initial_request(human_request, initial_status, triage)
    from hermes_cli.kanban_block_policy import creation_lint_error
    _lint = creation_lint_error(title, initial_status, human_request, triage=triage) if lint_blocked else None
    if _lint:
        raise ValueError(_lint)
    completion_contract = validate_contract(completion_contract)
    model_override, provider_override = _validate_model_override(model_override, provider_override)
    from hermes_cli.kanban_pin_reroute import mandatory_column
    pin_column = mandatory_column(pin_mandatory, model_override)
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}")
    # A project-scoped board anchors every new task to its project's repo
    # (deterministic worktree + branch) without each surface repeating it.
    # An explicit ``scratch`` (or ``project_id=""``) is a request for no project:
    # it must not be upgraded to a worktree in the board's repo (#106342).
    if project_id is None and workspace_kind != "scratch":
        try:
            project_id = (_board_meta_for(board).get("project_id") or "").strip() or None
        except Exception:
            pass
    if workspace_kind is None:
        workspace_kind = "scratch"
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")

    project_id, project_obj, project_repo, workspace_kind = _resolve_project_link(
        conn, project_id, project_source_task_id, workspace_kind, workspace_path
    )
    placement_policy, placement_audit, runs_on = resolved_db_placement(locals())
    parents = tuple(p for p in parents if p)
    skills_list = _normalize_task_skills(skills)

    if existing := native_admission.early_existing(conn, idempotency_key, created_by):
        return existing

    from hermes_cli.kanban_db_creation import validate_worktree
    validate_worktree(validate_workspace, workspace_kind, project_id, workspace_path, board)
    now = int(time.time())

    # Only persistent kinds inherit the board ``default_workdir``: a scratch
    # task inheriting it would point cleanup at the user's source tree.
    if workspace_path is None and project_repo is None and workspace_kind in {"dir", "worktree"}:
        board_default = _board_meta_for(board).get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    from hermes_cli.kanban_db_creation import project_branch_name, run_insert_guard, validate_create_assignment
    required, model_override, provider_override = validate_create_assignment(
        conn, assignee, runs_on=runs_on, requires=requires, workspace_kind=workspace_kind, project_id=project_id,
        workspace_path=workspace_path, skills=skills_list, validate_skills=validate_skills, project_repo=project_repo,
        require_declaration=require_declaration, model=model_override, provider=provider_override)
    from hermes_cli.kanban_pin_provider import reject_spaced_pin
    reject_spaced_pin(model_override, provider_override)

    from hermes_cli.kanban_lanes import prepare_classification
    _lane_classification = prepare_classification(conn, parents, initial_status, triage, tenant, locals())

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _sync.new_task_id(conn)
        try:
            # allow_nested: graph builders compose create_task under one outer
            # commit so the dispatcher never sees a half-built graph.
            with write_txn(conn, allow_nested=True):
                if existing := native_admission.existing(conn, idempotency_key):
                    return existing
                run_insert_guard(conn, insert_guard)
                task_status, tenant, lane = lane_state(conn, parents, initial_status, triage, tenant, locals())
                # Project worktree: fresh dir under the repo + deterministic
                # branch, instead of the random ``wt/<id>`` worker fallback.
                if project_obj is not None and workspace_kind == "worktree":
                    if project_repo and not workspace_path:
                        workspace_path = os.path.join(project_repo, ".worktrees", task_id)
                    if not branch_name:
                        branch_name = project_branch_name(project_obj, task_id, title)

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority, created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key, max_runtime_seconds,
                        skills, max_retries, model_override, provider_override, model_pin_mandatory, reasoning_effort,
                        goal_mode, goal_max_turns, session_id, completion_contract, requires, placement_policy,
                        admission_class, admission_frees, review_required
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, title.strip(), body, assignee, task_status, lane.priority(priority),
                        created_by, now, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key, _opt_int(max_runtime_seconds),
                        json.dumps(skills_list) if skills_list is not None else None,
                        _opt_int(max_retries), model_override, provider_override, pin_column,
                        reasoning_effort, 1 if goal_mode else 0, _opt_int(goal_max_turns), session_id, completion_contract,
                        json.dumps(required), placement_policy, admission_class, admission_frees, int(bool(review_required)),
                    ),
                )
                _sync.set_runs_on(conn, task_id, runs_on)  # placement labels (RFC "Placement"), synced boards only
                for pid in parents:
                    _link(conn, pid, task_id)
                from hermes_cli.kanban_db_creation import record_created
                record_created(
                    conn, task_id, assignee=assignee, status=task_status, parents=parents,
                    creator_task_id=creator_task_id, tenant=tenant, workspace_kind=workspace_kind, lane=lane,
                    workspace_path=workspace_path, branch_name=branch_name, project_id=project_id,
                    skills=skills_list, goal_mode=goal_mode, model_override=model_override,
                    provider_override=provider_override, created_by=created_by, requires=required,
                    human_request=human_request, placement_audit=placement_audit, placement_policy=placement_policy,
                    creation_receipt=creation_receipt, board=board)
                # ACK-edge: the originating channel hears a child BLOCK, not just the fan-in.
                initialize_ownership(conn, task_id, creator_task_id, parents, now, chat_origin, native_origin)
                if settle_owner_chat:  # every card leaves owned or is refused (kanban_owner_chat.settle) [daveey]
                    from hermes_cli.kanban_owner_chat import settle as settle_owner
                    settle_owner(conn, task_id, creator_task_id, created_by=created_by, strict=require_scope_authority, producer=owner_producer)
                native_admission.require_receipt(conn, task_id)
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
    raise RuntimeError("unreachable")


def _board_meta_for(board: Optional[str]) -> dict:
    return read_board_metadata(board if board else get_current_board())


def _link(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    from hermes_cli.kanban_dependency_validation import insert_link
    insert_link(conn, parent_id, child_id)


def _missing_task_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Subset of ``ids`` (order kept) with no ``tasks`` row."""
    ids = list(ids)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id FROM tasks WHERE id IN ({placeholders})", ids).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in ids if p not in present]


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
    "completed-desc": "completed_at DESC NULLS LAST, id DESC",
}


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign/reassign; raises RuntimeError while the task is running under a claim."""
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        from hermes_cli.kanban_capabilities import validate_assignment
        task = get_task(conn, task_id)
        validate_assignment(profile, requires=task.requires, workspace_kind=task.workspace_kind,
                            workspace_path=task.workspace_path, skills=task.skills)
        if row["assignee"] != profile:
            # The failure streak is per task/profile; a new profile starts fresh.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?", (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        # ``from`` lets the respawn guard tell a real handoff (dev→closer) from
        # a no-op re-assign or an unassign, which must not lift ``active_pr``.
        _append_event(
            conn, task_id, "assigned", {"assignee": profile, "from": row["assignee"]},
        )
    # Observer fires AFTER commit so subscribers see durable state.
    notify_task_updated(conn, task_id, ("assignee",))
    return True


def set_model_override(
    conn: sqlite3.Connection, task_id: str, model: Optional[str], provider: Optional[str] = None,
) -> bool:
    """Set (empty ``model`` clears BOTH) the per-task model/provider override.
    Allowed while ``running``: it applies on the NEXT dispatch, which is the
    rate-limit-recovery flow (set, then reclaim/retry)."""
    model, provider = _validate_model_override(model, provider)
    if model:
        from hermes_cli.kanban_model_assignment import validate_task_pin
        model, provider = validate_task_pin(conn, task_id, model, provider)
    return _set_task_override(
        conn, task_id,
        "UPDATE tasks SET model_override = ?, provider_override = ?, model_pin_mandatory = NULL WHERE id = ?",
        (model, provider),
        "model_override_set", {"model": model, "provider": provider},
        ("model_override", "provider_override"), archived_msg="cannot set model override",
    )


def _set_task_override(
    conn: sqlite3.Connection, task_id: str, sql: str, params: tuple, event_kind: str, payload: dict,
    changed_fields: tuple[str, ...], *, archived_msg: str,
) -> bool:
    """Per-task override write: refuse archived tasks, record ``event_kind``,
    then fire the task-updated observer AFTER commit (RFC #58548)."""
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None:
            return False
        if status == "archived":
            raise RuntimeError(f"{archived_msg} on archived task {task_id}")
        conn.execute(sql, (*params, task_id))
        _append_event(conn, task_id, event_kind, payload)
    notify_task_updated(conn, task_id, changed_fields)
    return True


def set_reasoning_effort(conn: sqlite3.Connection, task_id: str, effort: Optional[str]) -> bool:
    """Set (empty clears; ``"none"`` pins thinking OFF) the per-task reasoning
    effort. Independent of the model override so clearing one never resets the
    other; applies on the NEXT dispatch, so settable while running."""
    effort = normalize_reasoning_effort(effort)
    return _set_task_override(
        conn, task_id, "UPDATE tasks SET reasoning_effort = ? WHERE id = ?", (effort,),
        "reasoning_effort_set", {"reasoning_effort": effort},
        ("reasoning_effort",), archived_msg="cannot set reasoning effort",
    )


# --- Links ---

def link_tasks(
    conn: sqlite3.Connection,
    parent_id: str,
    child_id: str,
    *,
    expected_child_run_id: Optional[int] = None, allow_nested: bool = False,
) -> bool:
    """Link ``parent_id -> child_id``. Returns True when the link gated a
    ``ready`` child back to ``todo`` (the new parent is not yet terminal), so
    callers can surface the demotion instead of a silent status flip.

    A running child cannot normally be gated retroactively, so reject the edge
    rather than record a dependency that did not constrain the active run. The
    owning worker may link its own active run for a subsequent dependency-block
    handoff by supplying its trusted ``expected_child_run_id``.
    """
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    gated = False
    with write_txn(conn, allow_nested=allow_nested):
        from hermes_cli.kanban_dependency_validation import check_link_parent, missing_dependency_error
        missing = _missing_task_ids(conn, [parent_id, child_id])
        if missing:
            raise missing_dependency_error(missing)
        child = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (child_id,),
        ).fetchone()
        if child["status"] == "running" and (
            expected_child_run_id is None
            or child["current_run_id"] != expected_child_run_id
        ):
            raise ValueError(f"cannot link {parent_id} -> {child_id}: child is already running")
        check_link_parent(conn, parent_id, child_id)  # cycles; standing parents [daveey]
        _link(conn, parent_id, child_id)
        # Terminal restrictions must gate newly linked children too.
        if parent_blocks(conn, parent_id, _task_status(conn, parent_id)):
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
            gated = cur.rowcount == 1
            if gated:
                _append_event(
                    conn,
                    child_id,
                    "dependency_wait",
                    {"reason": "parent_not_done", "demoted": True, "parent": parent_id},
                )
        _append_event(
            conn,
            child_id,
            "linked",
            {"parent": parent_id, "child": child_id},
        )
        _inherit_notify_subs(conn, child_id, (parent_id,))
    return gated


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent_id, child_id),
        )
        removed = cur.rowcount > 0
        if removed:
            _append_event(conn, child_id, "unlinked", {"parent": parent_id, "child": child_id})
    if removed:
        # Re-gate the child now (as complete_task/unblock_task do) instead of
        # leaving it in todo until the next tick.
        recompute_ready(conn)
    return removed


def _linked_ids(conn: sqlite3.Connection, want: str, where: str, task_id: str) -> list[str]:
    rows = conn.execute(
        f"SELECT {want} FROM task_links WHERE {where} = ? ORDER BY {want}", (task_id,)
    ).fetchall()
    return [r[want] for r in rows]


# Dependency edge removed — re-evaluate promotion eligibility for the child immediately. Matches the
# contract of complete_task and unblock_task; without this the child stays stuck in todo until the next
# dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "parent_id", "child_id", task_id)


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    return _linked_ids(conn, "child_id", "parent_id", task_id)


def task_graph_contexts(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, dict]:
    """Bulk-load compact direct graph state for graph-aware diagnostics."""
    ordered_ids = list(dict.fromkeys(str(task_id) for task_id in task_ids if task_id))
    contexts = {task_id: {"parents": [], "children": []} for task_id in ordered_ids}
    if not ordered_ids:
        return contexts

    placeholders = ",".join("?" for _ in ordered_ids)
    for bucket, own, other in (("parents", "child_id", "parent_id"), ("children", "parent_id", "child_id")):
        for row in conn.execute(
            f"SELECT l.{own} AS owner_id, t.id, t.title, t.status, t.block_kind, t.human_request "
            f"FROM task_links l JOIN tasks t ON t.id = l.{other} "
            f"WHERE l.{own} IN ({placeholders}) ORDER BY l.{own}, t.id", tuple(ordered_ids),
        ).fetchall():
            contexts[row["owner_id"]][bucket].append(
                {"id": row["id"], "title": row["title"], "status": row["status"],
                 "block_kind": row["block_kind"], "human_request": _json_or(row["human_request"])}
            )
    return contexts


def task_graph_context(conn: sqlite3.Connection, task_id: str) -> dict:
    """Return compact direct parent/child state for one task."""
    return task_graph_contexts(conn, [task_id])[task_id]


# --- Comments & events ---

def record_comment_delivery(conn: sqlite3.Connection, task_id: str, run_id: int,
                            comment_ids: list[int]) -> None:
    """Receipt that the live bridge steered ``comment_ids`` into the running worker of ``run_id``."""
    with write_txn(conn, allow_nested=True):
        _append_event(conn, task_id, "comment_delivered",
                      {"comment_ids": [int(i) for i in comment_ids], "run_id": int(run_id)},
                      run_id=int(run_id))


def live_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """The active run id when the task is ``running``, else None."""
    row = conn.execute("SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row or row[0] != "running" or row[1] is None:
        return None
    return int(row[1])


def undelivered_live_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    """Comments posted since the live run started that the run neither wrote nor received."""
    run_id = live_run_id(conn, task_id)
    if run_id is None:
        return []
    started = conn.execute("SELECT started_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    since = int(started[0]) if started and started[0] is not None else 0
    skip = own_comment_ids(conn, task_id, run_id) | delivered_comment_ids(conn, task_id, run_id)
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY id ASC", (task_id, since),
    ).fetchall()
    return [Comment.from_row(r) for r in rows if int(r[0]) not in skip]


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
        raise ValueError(f"unknown task {task_id}")


def _task_rows(conn: sqlite3.Connection, table: str, task_id: str, order: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT * FROM {table} WHERE task_id = ? ORDER BY {order}", (task_id,)
    ).fetchall()


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    return [Comment.from_row(r) for r in _task_rows(conn, "task_comments", task_id, "created_at ASC")]


def list_comments_after(
    conn: sqlite3.Connection, task_id: str, *, after_id: int = 0
) -> list[Comment]:
    """Comments with ``id > after_id`` — keyed on rowid, not ``created_at``, so a
    same-second burst is never skipped (live worker comment bridge)."""
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? AND id > ? ORDER BY id ASC", (task_id, int(after_id)),
    ).fetchall()
    return [Comment.from_row(r) for r in rows]


# --- Attachments ---

class AttachmentTooLarge(ValueError):
    """Attachment over the size cap. A ``ValueError`` so generic 400 handlers
    still catch it while the tool/CLI can give a 413-style message."""


def store_attachment_bytes(
    conn: sqlite3.Connection, task_id: str, filename: str, data: bytes, *,
    content_type: Optional[str] = None, uploaded_by: Optional[str] = None,
    board: Optional[str] = None, max_bytes: Optional[int] = None,
) -> int:
    """Single attachment write path (dashboard, tools, CLI): size cap, safe
    basename, collision-free blob under :func:`task_attachments_dir`, then the
    metadata row. Raises :class:`AttachmentTooLarge` / ``ValueError``; a blob
    whose row insert fails is removed before re-raising. Returns the new id."""
    if max_bytes is None:
        max_bytes = KANBAN_ATTACHMENT_MAX_BYTES
    if len(data) > max_bytes:
        raise AttachmentTooLarge(f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit")
    from hermes_cli.kanban_db_attachment_names import _collision_free_path, _safe_attachment_name
    safe_name = _safe_attachment_name(filename)
    dest_dir = task_attachments_dir(task_id, board=board)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = _collision_free_path(dest_dir, safe_name)
    dest_path.write_bytes(data)
    try:
        return add_attachment(
            conn, task_id, filename=dest_path.name, stored_path=str(dest_path.resolve()),
            content_type=content_type, size=len(data), uploaded_by=uploaded_by,
        )
    except Exception:
        # Don't leave an orphan blob if the metadata insert fails (most
        # commonly: the task id doesn't exist).
        with contextlib.suppress(OSError):
            dest_path.unlink(missing_ok=True)
        raise


def add_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str,
    content_type: Optional[str] = None, size: int = 0, uploaded_by: Optional[str] = None,
) -> int:
    """Record the metadata row (+ ``attached`` event) for a blob the caller already wrote."""
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        _require_task(conn, task_id)
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, filename.strip(), stored_path, content_type, int(size), uploaded_by, now),
        )
        _append_event(
            conn, task_id, "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    return [Attachment.from_row(r) for r in _task_rows(conn, "task_attachments", task_id, "created_at ASC, id ASC")]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute("SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)).fetchone()
    return None if r is None else Attachment.from_row(r)


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete the row (source of truth) and best-effort its blob; None when no row matched."""
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        has_remaining_blob_reference = conn.execute(
            "SELECT 1 FROM task_attachments WHERE stored_path = ? LIMIT 1",
            (att.stored_path,),
        ).fetchone() is not None
        _append_event(conn, att.task_id, "attachment_removed", {"filename": att.filename})
    if not has_remaining_blob_reference:
        with contextlib.suppress(OSError):
            p = Path(att.stored_path)
            if p.is_file():
                p.unlink()
    return att


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    return [Event.from_row(r) for r in _task_rows(conn, "task_events", task_id, "created_at ASC, id ASC")]


def _insert_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str, created_at: int,
) -> None:
    """Raw comment INSERT for callers already inside a write txn (``add_comment``
    opens its own txn and emits ``commented``)."""
    conn.execute(
        "INSERT INTO task_comments (task_id, author, body, created_at) "
        "VALUES (?, ?, ?, ?)", (task_id, author, body, created_at),
    )


def _append_event(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict] = None, *,
    run_id: Optional[int] = None,
) -> None:
    """Insert an event row inside the caller's txn; ``run_id`` groups it by attempt (NULL = task-scoped)."""
    from hermes_cli.kanban_db_events import append_event
    append_event(conn, task_id, kind, payload, run_id=run_id)


from hermes_cli.kanban_db_run import _end_run


def _task_status(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Current ``tasks.status`` for ``task_id``, or ``None`` when no such row."""
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row["status"] if row else None


from hermes_cli.kanban_db_run import _current_run_id


# Distinguishes "caller named the acting profile" (which may legitimately be
# None for an unassigned card) from "read the card's current assignee".
_UNSET: Any = object()


def _end_or_synthesize_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, status: str,
    summary: Optional[str] = None, metadata: Optional[dict] = None, synthesize: bool,
    profile: Any = _UNSET,
) -> Optional[int]:
    """:func:`_end_run`; when no run was active and ``synthesize`` holds, record a
    zero-duration run instead so the handoff fields survive in attempt history.
    ``profile`` overrides the profile read off the task row for the synthesized
    run — transitions that reassign the task (e.g. review handoff) pass the
    acting profile captured before the rewrite."""
    run_id = _end_run(conn, task_id, outcome=outcome, status=status, summary=summary, metadata=metadata)
    _run_end_lease.note(conn, task_id, run_id)  # [daveey] free the Board lease after commit
    if run_id is None and synthesize:
        run_id = _synthesize_ended_run(conn, task_id, outcome=outcome, summary=summary, metadata=metadata, profile=profile)
    return run_id


def _synthesize_ended_run(
    conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str] = None,
    error: Optional[str] = None, metadata: Optional[dict] = None,
    profile: Any = _UNSET,
) -> int:
    """Zero-duration closed run for a terminal transition on a never-claimed
    task, so the handoff fields aren't silently dropped (``_end_run`` is a
    no-op then). ``started_at == ended_at`` keeps elapsed stats honest. Does
    NOT touch the tasks row.

    ``profile`` overrides the profile read off the task row: transitions that
    reassign the task (e.g. review handoff) pass the acting profile captured
    before the rewrite, so the run names the actor, not the new assignee."""
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if profile is _UNSET:
        profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id, profile, step_key, outcome, outcome, summary, error, _json_or_null(metadata),
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# --- Dependency resolution (todo -> ready) ---

def _latest_event(
    conn: sqlite3.Connection, task_id: str, kind: str, run_id: Optional[int] = None,
) -> Optional[sqlite3.Row]:
    from hermes_cli.kanban_db_events import latest_event

    return latest_event(conn, task_id, kind, run_id)


def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    """``review`` when the newest lifecycle event carries a review
    ``resume_status``/``retry_status``/``source_status``, else ``ready`` (legacy)."""
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind IN ("
        "'blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', 'scheduled', 'continued', 'promoted_manual', "
        "'unblocked', 'changes_requested', 'review_reopened', 'status', 'reclaimed', "
        "'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited', 'owner_restart_lost', 'contained', 'containment_released', 'containment_closed'"
        ") ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    payload = _json_dict(_row_get(row, "payload"))
    for key in ("resume_status", "retry_status", "source_status"):
        if payload.get(key) == "review":
            return "review"
    return "ready"


def recompute_ready(conn: sqlite3.Connection, failure_limit: int | None = None) -> int:
    """Promote dependency waits/recoverable failure stops; return count. Call OUTSIDE a write txn.
    Opens an IMMEDIATE txn. Agent-triage failure stops recover like blocked stops; other holds need resolution.
    ``recovery_held`` preserves sticky blocks and breaker limits (max_retries > failure_limit > default).
    Dependency-review release is prepared outside the txn (no GitHub reads for held cards), fenced inside.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    from hermes_cli.kanban_block_routing import recovery_held
    from hermes_cli.kanban_wake_check import foreign_ids
    from hermes_cli import kanban_review_release
    from hermes_cli import kanban_spend_decision as spend_decision
    from hermes_cli import kanban_db_graph as _graph
    promoted = 0
    remote = _sync.has_remote_parents(conn)
    releases = kanban_review_release.prepare(conn, failure_limit)
    with write_txn(conn):
        copies, fresh = foreign_ids(conn), {}  # fresh: the mirror-freshness verdict, read once per pass
        from hermes_cli.kanban_lane_daily_limits import release_due
        promoted += release_due(conn, foreign=copies)
        todo_rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries "
            "FROM tasks WHERE status IN ('todo', 'blocked') OR ("
            "status = 'triage' AND block_kind = 'transient' AND 'gave_up' = ("
            "SELECT kind FROM task_events WHERE task_id = tasks.id AND kind IN ("
            "'gave_up', 'blocked', 'block_loop_detected', 'supervisor_escalated', 'status') "
            "ORDER BY id DESC LIMIT 1))"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            # The home gateway owns readiness and missing-parent maintenance.
            # A retained worker copy may still claim ready work, but must not
            # publish a second promotion or expire the owner's missing streak.
            if task_id in copies or _containment.held(conn, task_id):
                continue
            # A spend stop leaves only on a recorded decision (kanban_spend_decision.pending). The triage clause
            # above would read an older ``gave_up`` as the stop: promote here, re-triage in the hold, every tick.
            if spend_decision.pending(conn, task_id):
                continue
            cur_status = _graph.clear_found_parents(conn, row, remote=remote)  # parent_missing block, parents back: todo
            if cur_status == row["status"] and recovery_held(conn, row, failure_limit):
                continue
            states = parent_states(conn, task_id, remote=remote)
            # Inside this txn, for a ``todo`` child: start/close a missing-parent streak (``parent_missing`` /
            # ``parent_found``) and block after PARENT_MISSING_BLOCK_SECONDS (synced: replica at the feed head only).
            if cur_status == "todo" and _graph.note_missing_parents(conn, task_id, states, remote=remote, fresh=fresh):
                continue
            if not any(parent_blocks(conn, pid, st) for pid, st in states):
                resume_status = _resume_status_from_events(conn, task_id)
                promotion_payload = kanban_review_release.promotion_payload(conn, task_id, resume_status, releases)
                if cur_status in ("blocked", "triage"):
                    conn.execute(
                        "UPDATE tasks SET status = ? "
                        "WHERE id = ? AND status = ?", (resume_status, task_id, cur_status),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = ? WHERE id = ? AND status = 'todo'",
                        (resume_status, task_id),
                    )
                _append_event(
                    conn, task_id, "promoted",
                    promotion_payload,
                )
                promoted += 1
    _graph.report_deferred_pass(conn, fresh)  # one INFO line per board per stale replica stamp
    return promoted


# --- Claim / complete / block ---

# A child whose parent row this board does not have (neither a local card nor, on a synced board, a card another
# gateway owns) waits; after this long it is blocked "parent <id> missing from this board". [daveey]
PARENT_MISSING_BLOCK_SECONDS = 600


from hermes_cli.kanban_db_parent_states import parent_blocks, parent_states


def _parents_satisfied(conn: sqlite3.Connection, task_id: str) -> bool:
    """Mirror hermes-hub worker/src/transitions.ts parentsSatisfied: terminal, unrestricted parents only.
    Foreign parents use the Board mirror; missing parents and containment archives gate."""
    return not any(parent_blocks(conn, pid, st) for pid, st in parent_states(conn, task_id))


def unsatisfied_parents(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, str]]:
    """Read-only blockers in id order; terminal restrictions carry a distinct ``:restricted`` label."""
    return [(pid, f"{st}:restricted" if st in ("done", "archived") else st or "missing")
            for pid, st in parent_states(conn, task_id) if parent_blocks(conn, pid, st)]


def claim_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None, admission_check=None, expect_assignee: Optional[str] = None,
) -> Optional[Task]:
    """Atomic ``ready -> running``: the claimed ``Task``; ``None`` if already claimed, not ``ready``, no
    longer ``expect_assignee``'s (#147534), or the write-free ``admission_check()`` (run in the claim txn
    before any write) refused it. [daveey]"""
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    # [daveey] Spend decisions are home-owned, but every executor must honour their hold before taking a lease.
    spend_check = _spend_claim.claim_check(conn, task_id)
    if _containment.held(conn, task_id) or spend_check() or admission_hold_set(task_id) or hub_cpu_advisory(task_id, conn=conn):
        return None
    gate = _sync.claim_gate(conn, task_id, expires)
    if gate.deferred:
        return None
    with gate, claim_transaction(conn, task_id=task_id, gate=gate) as admitted:
        if not admitted or _containment.held(conn, task_id) or spend_check() or (admission_check is not None and admission_check()):  # [daveey] same txn as the CAS
            return None
        # Never ready -> running with an undone parent; recompute_ready re-promotes when parents finish.
        if not _parents_satisfied(conn, task_id):
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'", (task_id,),
            )
            _append_event(conn, task_id, "claim_rejected", {"reason": "parents_not_done"})
            return None
        # Close a leaked prior run so the CAS below doesn't strand it.
        _reclaim_dangling_run(
            conn, task_id, statuses=("ready",), now=now, note="invariant recovery on re-claim",
        )
        run_id = claim_and_open_run(conn, task_id, "ready", lock, gate.expires, now, gate=gate,
                                    expect_assignee=expect_assignee)
        if run_id is None:
            return None
        claimed = get_task(conn, task_id)
    _fire_task_hook("kanban_task_claimed", claimed, task_id, run_id)
    return claimed


def claim_review_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None, admission_check=None, expect_assignee: Optional[str] = None,
) -> Optional[Task]:
    """Atomic ``review -> running`` (None when lost). Parents are re-checked
    (one may have reopened meanwhile) and a NEW run tracks the reviewer
    separately from the implementer. ``admission_check``: as :func:`claim_task`."""
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    spend_check = _spend_claim.claim_check(conn, task_id)
    if _containment.held(conn, task_id) or spend_check() or admission_hold_set(task_id) or hub_cpu_advisory(task_id, conn=conn):  # [daveey] before the Board-service lease (see claim_task)
        return None
    gate = _sync.claim_gate(conn, task_id, expires, lane="review")
    if gate.deferred:
        return None
    with gate, claim_transaction(conn, task_id=task_id, gate=gate) as admitted:
        if not admitted or _containment.held(conn, task_id) or spend_check() or (admission_check is not None and admission_check()):  # [daveey] same txn as the CAS
            return None
        if not _parents_satisfied(conn, task_id):
            demoted = conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'review' AND claim_lock IS NULL", (task_id,),
            )
            if demoted.rowcount == 1:
                _append_event(
                    conn, task_id, "dependency_wait",
                    {"reason": "parent_reopened", "source_status": "review"},
                )
            return None
        run_id = claim_and_open_run(
            conn, task_id, "review", lock, gate.expires, now, event_extra={"source_status": "review"}, gate=gate,
            expect_assignee=expect_assignee,
        )
        if run_id is None:
            return None
        return get_task(conn, task_id)


def _retry_status_for_run(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int] = None,
) -> str:
    """``review`` when the run's ``claimed`` event says ``source_status=review``,
    else ``ready`` — one place, so crash/timeout/reclaim can't silently turn a
    reviewer run into an implementation run."""
    if run_id is None:
        run_id = _current_run_id(conn, task_id)
    if run_id is None:
        return "ready"
    event = _latest_event(conn, task_id, "claimed", run_id)
    payload = _json_dict(_row_get(event, "payload"))
    return "review" if payload.get("source_status") == "review" else "ready"


# Run outcome -> lifecycle status a goal loop should report for a handed-off run.
_RUN_OUTCOME_TERMINAL_STATUS = {
    "completed": "done",
    "review_requested": "review",
    "changes_requested": "changes_requested",
    "blocked": "blocked",
    "dependency_wait": "blocked",
    # The run's own checkpoint (kanban_continuation) or timed park stops its loop; a park is no ownership loss.
    "continued": "continued", "scheduled": "scheduled", "contained": "contained",  # [daveey]
}


def goal_run_status(
    conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int] = None,
) -> Optional[str]:
    """Lifecycle status as seen by ONE run: terminal handoffs bind to that run,
    any other ownership loss is ``superseded`` — otherwise an old goal loop
    would read the successor's live ``running`` and mutate it."""
    task = get_task(conn, task_id)
    if task is None:
        return None
    if expected_run_id is not None:
        row = conn.execute(
            "SELECT outcome FROM task_runs WHERE id = ? AND task_id = ?",
            (int(expected_run_id), task_id),
        ).fetchone()
        outcome = str(row["outcome"]) if row and row["outcome"] is not None else None
        terminal_status = _RUN_OUTCOME_TERMINAL_STATUS.get(outcome)
        if terminal_status is not None:
            return terminal_status
        if outcome is not None or task.current_run_id != int(expected_run_id):
            return "superseded"
    if task.status in {"ready", "todo"}:
        event = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? AND kind != 'dispatch_skipped' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        if event and event["kind"] == "changes_requested":
            return "changes_requested"
    return task.status


def release_stale_claims(
    conn: sqlite3.Connection, *, signal_fn=None, failure_limit: Optional[int] = None,
) -> int:
    """Reclaim ``running`` tasks whose claim expired; returns the count reclaimed.

    Every reclaim that actually releases a claim is a non-success attempt and
    is booked through ``_record_task_failure`` (#111306): a claim that expired
    without a worker ever spawning otherwise loops claim -> reclaim -> claim
    with ``consecutive_failures`` stuck at 0, so the breaker never trips.
    ``reclaim_task`` (operator path) deliberately resets the counter instead.

    A host-local worker that is still alive gets its claim *extended* instead
    (a slow model can sit longer than the TTL inside one tool-free call, so no
    heartbeat) — unless ``last_heartbeat_at`` is older than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (wedged; ``_touch_activity``
    keeps any genuinely active worker fresh). Safe to call often.

    Reclaiming a live worker mid-flight produces the spawn- then-immediately-reclaim loop seen on slow
    models that spend longer than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM call (#23025):
    no tool calls means no ``kanban_heartbeat``, even though the subprocess is healthy.
    Backstop (#29747 gap 3): if the worker's PID is still alive but its ``last_heartbeat_at`` is stale by
    more than ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has been making no observable
    progress and we reclaim anyway — even if ``_pid_alive`` is still true. This catches the
    wedged-in-a-logic-loop case where the process is technically running but accomplishing nothing.
    ``_touch_activity`` (run_agent.py) bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
    so any genuinely active worker keeps its heartbeat fresh as a side effect of normal API traffic.
    ``enforce_max_runtime`` and ``detect_crashed_workers`` remain the upper bounds for genuinely wedged or
    dead workers.
    """
    from hermes_cli.kanban_run_end_retry import drain
    drain(conn)
    now = int(time.time())
    reclaimed = 0
    host_prefix = _host_prefix()
    from hermes_cli.kanban_suspend_ledger import awake_seconds  # [daveey]
    stale = conn.execute(
        "SELECT t.id, t.current_run_id, t.claim_lock, t.worker_pid, t.worker_started_at, t.claim_expires, "
        "       t.last_heartbeat_at, t.assignee, r.started_at AS run_started_at "
        "FROM tasks t LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.claim_expires IS NOT NULL "
        "  AND t.claim_expires < ?", (now,),
    ).fetchall()
    from hermes_cli.kanban_owner_recovery import owner_reclaim_blocked, owner_reclaim_blocked_after_kill, reclaim_error, terminate_modern_bindings
    for row in stale:
        if owner_reclaim_blocked(conn, row["id"], for_termination=True, selected=row):
            continue
        host_local = _claim_is_local(row["claim_lock"], host_prefix)
        hb = _to_epoch(row["last_heartbeat_at"])
        # Backstop: a heartbeat older than the max-stale threshold means no
        # observable progress — reclaim even if the PID is alive (logic loop).
        # [daveey] Measured from the newer of the heartbeat and the active run's start (a fresh run
        # is never stale against its predecessor's heartbeat), counting awake time only: a host
        # suspend recorded by the dispatcher (``kanban_suspend_ledger``) is not worker silence.
        run_started = _to_epoch(_row_get(row, "run_started_at"))
        hb_ref = max(hb, run_started) if hb is not None and run_started is not None else hb
        hb_awake_age = awake_seconds(conn, hb_ref, now) if hb_ref is not None else None
        heartbeat_stale = hb_awake_age is not None and hb_awake_age > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        started_at = _row_get(row, "worker_started_at")
        live = host_local and row["worker_pid"] and _worker_alive(row["worker_pid"], started_at) and not heartbeat_stale
        lease_lost = _sync.lease_lost(conn, row["id"], now, _resolve_claim_ttl_seconds() if live else None)  # [daveey] asks the Board
        if (_stale_claim_kept(conn, row, now, live=live and not lease_lost, host_local=host_local, started_at=started_at)
                or terminate_modern_bindings(conn, row, signal_fn) is not None):
            continue
        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn, started_at=started_at,
        )
        # A live worker of ours must keep its claim (else a duplicate spawns beside it).
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, row["id"], row["claim_lock"], now, termination,
                reason="ttl_expired_worker_alive",
            )
            continue
        # [daveey] Expired primary leases are finalized by the Board alarm.
        # Local termination is still necessary, but publishing a competing
        # reclaim/counter would overwrite that authoritative result after sleep.
        if _sync.board_mode(conn) == "primary":
            continue
        with write_txn(conn):
            # Again under the write lock: an interpreter that bound since the check above
            # changes neither claim_lock nor worker_pid, so the CAS below cannot see it.
            if owner_reclaim_blocked_after_kill(conn, row["id"], signalled=bool(termination.get("termination_attempted"))):
                continue
            retry_status, error = _retry_status_for_run(conn, row["id"]), reclaim_error(conn, row["id"], row["claim_lock"])
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ? "
                # A worker that registered its own pid since the SELECT keeps its claim.
                "AND worker_pid IS ? AND current_run_id IS ?",
                (retry_status, row["id"], row["claim_lock"], now, row["worker_pid"], row["current_run_id"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _record_reclaim(
                conn, row["id"], termination, outcome="lease_lost" if lease_lost else "reclaimed",
                error=error,
                payload={
                    "stale_lock": row["claim_lock"],
                    "worker_pid": _opt_int(row["worker_pid"]),
                    "claim_expires": _to_epoch(row["claim_expires"]),
                    "last_heartbeat_at": hb,
                    "now": now,
                    "host_local": host_local,
                    "heartbeat_stale": bool(heartbeat_stale),
                    "heartbeat_awake_age": _opt_int(hb_awake_age),  # [daveey]
                    "retry_status": retry_status,
                    "failure_notice": "canonical",
                },
            )
            _record_task_failure(
                conn, row["id"], error, run_id=run_id,
                outcome="reclaimed", failure_limit=failure_limit,
                release_claim=False, end_run=False, allow_nested=True,
                event_payload_extra={"worker_pid": _opt_int(row["worker_pid"]), "retry_status": retry_status},
            )
            reclaimed += 1
        # Post-commit observer; every non-reclaim branch ``continue``d above.
        if _kanban_observer_consumed("on_kanban_worker_stale_claim"):
            _fire_kanban_lifecycle_hook(
                "on_kanban_worker_stale_claim", row["id"], board=get_current_board(),
                assignee=row["assignee"], run_id=run_id, worker_pid=_opt_int(row["worker_pid"]),
                heartbeat_stale=bool(heartbeat_stale), retry_status=retry_status,
            )
    return reclaimed


def _record_reclaim(
    conn: sqlite3.Connection, task_id: str, termination: dict, *, error: str, payload: dict,
    outcome: str = "reclaimed",
) -> Optional[int]:
    """Close the active run as ``reclaimed`` and emit the ``reclaimed`` event
    (payload merged with the termination report). Caller holds the txn."""
    run_id = _end_run(
        conn, task_id, outcome=outcome, status=outcome, error=error, metadata=termination,
    )
    payload.update(termination)
    _append_event(conn, task_id, "reclaimed", payload, run_id=run_id)
    return run_id


def reclaim_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None, signal_fn=None,
    urgency_source: Optional[str] = None, interrupt_target: Optional[str] = None,
) -> bool:
    """Release a claim and restore its phase; updater pauses preserve prior failures.

    Other operator reclaims reset the failure counter. False when not running.
    Bad attribution raises first, before any signal or claim change."""
    attribution = _cu_requeue.reclaim_attribution(reason, urgency_source, interrupt_target)
    from hermes_cli.kanban_manual_reclaim import reclaim_task as reclaim
    return reclaim(conn, task_id, reason=reason, signal_fn=signal_fn, attribution=attribution)


def reassign_task(
    conn: sqlite3.Connection, task_id: str, profile: Optional[str], *, reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign (None unassigns); a running task is refused unless
    ``reclaim_first`` releases its claim — the "this profile's model is broken" path."""
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _verify_created_cards(
    conn: sqlite3.Connection, completing_task_id: str, claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom). Verified = the row
    exists AND ``created_by`` is the completing task's assignee or id, OR the
    card is linked as its child (created elsewhere, attached by the worker).
    Never mutates."""
    ordered = list(dict.fromkeys(str(x).strip() for x in (claimed_ids or []) if str(x).strip()))
    if not ordered:
        return [], []

    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,)).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})", tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))
    # A Board-synced live file is partial: ask the Board replica for the rest (same trust rule). [daveey]
    remote, remote_children = _replica_tasks([c for c in ordered if c not in found], completing_task_id)
    found.update(remote)
    linked_children |= remote_children

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        trusted = created_by is not None and (
            (completing_assignee and created_by == completing_assignee)
            or created_by == completing_task_id
            or cid in linked_children
        )
        (verified if trusted else phantom).append(cid)
    return verified, phantom


from hermes_cli.kanban_prose_refs import _TASK_ID_PROSE_RE, _scan_prose_for_phantom_ids  # re-exported
from hermes_cli.kanban_board_replica_refs import replica_tasks as _replica_tasks, unresolved_after_replica


class HallucinatedCardsError(ValueError):
    """``complete_task`` refused: ``created_cards`` has ids that don't exist or
    weren't created by this worker (``.phantom``). A ``ValueError`` so tool
    error handlers treat it as recoverable."""

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class EmptyCompletionError(ValueError):
    """``complete_task`` refused: no substantive ``result``, ``summary``, or
    stored result. A ``ValueError`` so tool error handlers treat it as
    recoverable. Review approvals are exempt (the human is the record)."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        super().__init__(
            f"completion blocked: {task_id} has no result or summary evidence"
        )


class ArtifactPreservationError(RuntimeError):
    """Raised when a declared scratch deliverable cannot be preserved."""


class LiveClaimError(ValueError):
    """``complete_task`` refused: the task is ``running`` under a live claim and
    the caller neither owns its run (``expected_run_id``) nor passed ``force``.
    Completing anyway would close the worker's run row underneath a process
    that is still executing. A ``ValueError`` so tool error handlers treat it
    as recoverable."""

    def __init__(self, task_id: str):
        super().__init__(
            f"{task_id} is running under a live worker claim; pass expected_run_id "
            "(worker ownership) or force=True (explicit operator override) instead "
            "of closing the live run"
        )


def _claim_is_live(trow) -> bool:
    """True when a ``running`` task's claim still protects a run: its spawned worker exists (PID + start-time
    fingerprint). TTL is not consulted: the process is the liveness authority (``reclaim_stale_tasks`` extends it)."""
    return bool(trow["status"] == "running" and trow["claim_lock"] is not None
                and trow["worker_pid"] and _worker_alive(trow["worker_pid"], trow["worker_started_at"]))


@source_receipt_preflight  # workspace_source: the ending's receipt (published/unchanged/retained), S6
def complete_task(
    conn: sqlite3.Connection, task_id: str, *, result: Optional[str] = None,
    summary: Optional[str] = None, metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None, expected_run_id: Optional[int] = None,
    fire_lifecycle_hook: bool = True, force: bool = False, goal_verification=None,
    placement_settled: bool = False,
) -> bool:
    """``running|ready|blocked|review -> done``; records ``result``.

    ``ready`` is accepted for manual CLI completion, ``review`` for human
    approval. A ``running`` task under a live claim is only completed with
    proof of ownership (``expected_run_id``) or ``force=True`` (explicit
    operator override) — otherwise :class:`LiveClaimError`, the same fence
    :func:`request_review` applies. With no active run the handoff fields survive via
    :func:`_synthesize_ended_run`. ``summary`` (defaults to ``result``) and
    ``metadata`` land on the closing run for :func:`build_worker_context`.
    ``created_cards`` are verified first — a phantom id raises
    :class:`HallucinatedCardsError` after an auditable event; afterwards the
    prose is scanned for unresolvable ``t_<hex>`` refs (advisory event only).
    Completions from non-review statuses need evidence: a stripped ``result``
    or ``summary``, or a stripped result already stored on the card. Empty or
    whitespace-only evidence raises :class:`EmptyCompletionError` after an
    auditable event. Approving a card out of ``review`` stays exempt.
    """
    from hermes_cli.kanban_review_required import require_review
    from hermes_cli.kanban_containment_authority import require_acknowledgement
    from hermes_cli.kanban_assignment_routing import validate_repair_completion
    from hermes_cli.kanban_assignment_original import prepare as prepare_repair
    if not placement_settled:  # a settling placement stub cannot resume/retain; gating it would park forever
        require_acknowledgement(conn, task_id, metadata)
    require_review(conn, task_id, expected_run_id, placement_settled=placement_settled)
    from hermes_cli import kanban_completion_receipt as receipt_admission
    metadata, receipt_owners = receipt_admission.prepare(conn, task_id, metadata, summary=summary or result)
    now = int(time.time())
    if not _parents_satisfied(conn, task_id):
        return False
    from hermes_cli.kanban_pr_acceptance_store import prepare_acceptance, record_acceptance
    verified_cards = _gate_created_cards(conn, task_id, created_cards, summary or result)
    _gate_empty_completion(conn, task_id, result=result, summary=summary)
    metadata = _merge_completion_prose_artifacts(
        conn, task_id, metadata, summary=summary, result=result,
    )
    handoff_summary = summary if summary is not None else result
    acceptance = prepare_acceptance(conn, task_id, expected_run_id, metadata)
    if acceptance is False:
        return False
    from hermes_cli.kanban_metadata_files import discard_on_error
    staged, repair_observation = [], prepare_repair(conn, task_id)
    with _prestaged(conn, task_id, metadata), discard_on_error(staged), write_txn(conn):
        if not placement_settled:
            require_acknowledgement(conn, task_id, metadata)
        require_review(conn, task_id, expected_run_id, placement_settled=placement_settled)
        if not _parents_satisfied(conn, task_id):
            return False
        receipt_admission.recheck(conn, task_id, receipt_owners)
        validate_repair_completion(conn, task_id, observation=repair_observation)
        from hermes_cli.kanban_goal_receipt import record as record_goal
        goal_event_id = record_goal(conn, task_id, goal_verification, (summary or result or '').strip())
        acceptance_event_id = record_acceptance(conn, task_id, acceptance) if acceptance is not None else None
        if acceptance is not None and not acceptance_event_id:
            return False
        trow = conn.execute(
            "SELECT status, claim_lock, worker_pid, worker_started_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        prior_status = trow["status"] if trow else None
        # Refuse to close a LIVE worker's run without proof of ownership
        # (expected_run_id) or an explicit human override (force=True); see
        # _claim_is_live for what "live" means.
        if expected_run_id is None and not force and trow and _claim_is_live(trow):
            raise LiveClaimError(task_id)
        sql = """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL, claim_expires = NULL, worker_pid = NULL,
                       block_kind   = NULL,
                       human_request = NULL,
                       block_recurrences = 0
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked', 'review')
                """
        params: tuple = (result, now, task_id)
        if force:  # [daveey] --force also closes a parked (e.g. placement-forwarded) card; its copy is archived below
            sql = sql.replace("'review')", "'review', 'scheduled')")
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if _sr.with_source_receipt(conn, task_id, lambda: conn.execute(sql, params).rowcount) != 1:
            return False
        if isinstance(metadata, dict):
            staged.extend(_stage_completion_artifacts(conn, task_id, metadata, now))
        run_id = _end_run(conn, task_id, outcome="completed", status="done", summary=handoff_summary,
                          metadata=metadata)
        _run_end_lease.note(conn, task_id, run_id)  # [daveey] free the Board lease after commit
        # Never-claimed task: synthesize a run so the handoff fields survive.
        if run_id is None and (summary or metadata or result or prior_status == "review"):
            synth_summary, synth_metadata = handoff_summary, metadata
            if prior_status == "review" and not synth_summary and not synth_metadata:
                synth_summary = _REVIEW_APPROVED_NOTE
                synth_metadata = {"source_status": "review", "approval": "manual"}
            run_id = _synthesize_ended_run(
                conn, task_id, outcome="completed", summary=synth_summary, metadata=synth_metadata)
        event_summary = handoff_summary
        if prior_status == "review" and not event_summary:
            event_summary = _REVIEW_APPROVED_NOTE
        from hermes_cli.kanban_db_completion_payload import completed_event_payload
        completion_payload = completed_event_payload(result, event_summary, verified_cards, metadata, acceptance)
        if isinstance(metadata, dict) and "completion_receipt" in metadata:
            completion_payload["completion_receipt_admission"] = 1
        if goal_event_id is not None:
            conn.execute('UPDATE task_events SET run_id=? WHERE id=? AND task_id=?',
                         (run_id, goal_event_id, task_id))
            completion_payload['goal_verified_event_id'] = goal_event_id
            # The preview above hides FINAL SEND blocks and is clipped; the origin ledger must
            # compare the exact evidence the judge saw.
            completion_payload['goal_evidence'] = (summary or result or '').strip()
        if acceptance_event_id is not None:
            conn.execute("UPDATE task_events SET run_id=? WHERE id=? AND task_id=?",
                         (run_id, acceptance_event_id, task_id))
            completion_payload["pr_acceptance_event_id"] = acceptance_event_id
        from hermes_cli.kanban_completion_refs import stamp_uids
        stamp_uids(conn, completion_payload)  # [daveey] ids are per board file; the uid survives the Board feed
        _append_event(conn, task_id, "completed", completion_payload, run_id=run_id)
        _placement_fwd.record_cancel_intent(conn, task_id, "completed", settled=placement_settled)  # [daveey]
    _flag_phantom_prose_refs(conn, task_id, run_id, summary, result, verified_cards)
    _dup_detector.note_completion(conn, task_id, summary, result)  # [daveey] counts same-creator duplicates
    _placement_fwd.forward_completion(conn, task_id, settled=placement_settled)
    _clear_failure_counter(conn, task_id)  # success wipes the breaker counter (history stays on the event log)
    recompute_ready(conn)  # separate txn so children see ``done``
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_task_hook("kanban_task_completed", _done_task, task_id, run_id, summary=handoff_summary)
    return True


# Capability belongs to this loaded writer, not to helper files newly on disk.
complete_task.completion_receipt_admission = 1
_REVIEW_APPROVED_NOTE = "Review approved without additional evidence."


def _gate_created_cards(
    conn: sqlite3.Connection, task_id: str, created_cards: Optional[Iterable[str]], preview_text: Optional[str],
) -> list[str]:
    """Verify ``created_cards`` BEFORE the main write txn; returns the verified
    ids. A phantom id is recorded in its own tiny txn (auditable) then raised
    as :class:`HallucinatedCardsError` without touching task state."""
    if not created_cards:
        return []
    verified_cards, phantom_cards = _verify_created_cards(conn, task_id, created_cards)
    if phantom_cards:
        with write_txn(conn):
            _append_event(
                conn, task_id, "completion_blocked_hallucination",
                {
                    "phantom_cards": phantom_cards,
                    "verified_cards": verified_cards,
                    "summary_preview": _first_line(preview_text, 200) or None,
                },
            )
        raise HallucinatedCardsError(phantom_cards, task_id)
    return verified_cards


def _substantive_text(value: Optional[str]) -> bool:
    return bool(value is not None and str(value).strip())


from hermes_cli.kanban_completion_evidence import gate_empty_completion as _gate_empty_completion
from hermes_cli.kanban_artifact_prestage import prestaged as _prestaged  # large files are read before the write lock


from hermes_cli.kanban_metadata_files import stage_completion_artifacts as _stage_completion_artifacts


from hermes_cli.kanban_metadata_files import cleaned_artifact_paths as _cleaned_artifact_paths


def _flag_phantom_prose_refs(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int],
    summary: Optional[str], result: Optional[str], verified_cards: list[str],
) -> None:
    """Advisory post-commit scan of summary+result for unresolvable ``t_<hex>``
    references; emits ``suspected_hallucinated_references`` in its own txn so
    the completion is already durable. Never blocks."""
    scan_text = " ".join(filter(None, [summary, result]))
    if not scan_text:
        return
    phantom_refs = [p for p in _scan_prose_for_phantom_ids(conn, scan_text) if p not in set(verified_cards)]
    if phantom_refs:
        with write_txn(conn):
            _append_event(
                conn, task_id, "suspected_hallucinated_references",
                {"phantom_refs": phantom_refs, "source": "completion_summary"}, run_id=run_id,
            )


from hermes_cli.kanban_metadata_files import merge_completion_prose_artifacts as _merge_completion_prose_artifacts


def _persist_scratch_completion_artifacts(
    conn: sqlite3.Connection, task_id: str, metadata: dict,
) -> None:
    """Copy scratch-workspace completion artifacts before cleanup removes them."""
    raw_artifacts = metadata.get("artifacts")
    if not isinstance(raw_artifacts, (list, tuple)):
        return

    workspace = _scratch_workspace(conn, task_id)
    if workspace is None:
        return
    is_managed, board = _managed_scratch_path_info(workspace)
    if not is_managed:
        return

    try:
        workspace_root = workspace.resolve()
    except OSError:
        return

    attachment_dir = task_attachments_dir(task_id, board=board)
    persisted: list[str] = []
    used_destinations: set[Path] = set()
    changed = False

    def _discard_copies() -> None:
        _discard_staged_copies(used_destinations, attachment_dir)

    for item in raw_artifacts:
        artifact = str(item).strip() if isinstance(item, str) else ""
        if not artifact:
            continue
        src = Path(artifact).expanduser()
        try:
            resolved_src = src.resolve()
        except OSError:
            persisted.append(artifact)
            continue

        if not resolved_src.is_relative_to(workspace_root):
            persisted.append(artifact)
            continue

        problem = None
        if not src.is_file():
            problem = f"declared scratch artifact is unavailable or not a regular file: {artifact}"
        elif resolved_src.stat().st_size > KANBAN_ATTACHMENT_MAX_BYTES:
            problem = (
                f"declared scratch artifact exceeds the "
                f"{KANBAN_ATTACHMENT_MAX_BYTES}-byte limit: {artifact}"
            )
        if problem:
            _discard_copies()
            raise ArtifactPreservationError(problem)

        dest: Optional[Path] = None
        try:
            attachment_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_attachment_path(attachment_dir, resolved_src.name, used_destinations)
            _copy_capped(resolved_src, dest, artifact)
        except Exception as exc:
            if dest is not None:
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
            _discard_copies()
            if isinstance(exc, ArtifactPreservationError):
                raise
            raise ArtifactPreservationError(
                f"could not preserve declared scratch artifact {artifact}: {exc}"
            ) from exc
        used_destinations.add(dest)
        persisted.append(str(dest.resolve()))
        changed = True

    if changed:
        metadata["artifacts"] = persisted
        metadata["_staged_artifacts"] = [
            path for path in persisted if path.startswith(str(attachment_dir.resolve()))
        ]


def _discard_staged_copies(copies: Iterable[Path], attachment_dir: Path) -> None:
    """Remove staged attachment copies whose DB rows never committed; a leaked
    copy would make the retry stage ``name_1.ext`` next to an orphan."""
    for copied in copies:
        with contextlib.suppress(OSError):
            Path(copied).unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        attachment_dir.rmdir()


def _copy_capped(src: Path, dest: Path, artifact: str) -> None:
    """Chunked copy that aborts if the file grows past the attachment cap mid-copy."""
    with src.open("rb") as source_file, dest.open("xb") as destination_file:
        copied = 0
        while chunk := source_file.read(1024 * 1024):
            copied += len(chunk)
            if copied > KANBAN_ATTACHMENT_MAX_BYTES:
                raise ArtifactPreservationError(
                    f"declared scratch artifact grew beyond the size limit: {artifact}"
                )
            destination_file.write(chunk)


def _insert_completion_attachment(
    conn: sqlite3.Connection, task_id: str, *, filename: str, stored_path: str, size: int,
    created_at: int, uploaded_by: str = "kanban_complete",
) -> None:
    """Record a worker-produced artifact in the existing attachment table."""
    conn.execute(
        "INSERT INTO task_attachments "
        "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (task_id, filename, stored_path, size, uploaded_by, created_at),
    )
    _append_event(conn, task_id, "attached", {"filename": filename, "size": size, "by": uploaded_by})


def _unique_attachment_path(directory: Path, filename: str, used: set[Path]) -> Path:
    """Return a non-conflicting path under ``directory`` for ``filename``."""
    safe_name = Path(filename).name or "artifact"
    stem, suffix = Path(safe_name).stem or "artifact", Path(safe_name).suffix
    candidate = directory / safe_name
    idx = 1
    while candidate in used or candidate.exists():
        candidate = directory / f"{stem}_{idx}{suffix}"
        idx += 1
    return candidate


def _has_probe(wake_check: Optional[str], clock_only: bool) -> bool:
    return bool(wake_check and str(wake_check).strip()) or bool(clock_only)


def _probe_parks(kind: Optional[str], wake_check: Optional[str], clock_only: bool,
                 human_request: Optional[dict]) -> bool:
    """An agent-owned block (no kind, capability, transient, needs_input) with no human ask that
    brings a wake_check / clock_only is a wait on that probe: park it scheduled so the probe runs.
    [daveey]"""
    from hermes_cli.kanban_block_policy import AGENT_OWNED_KINDS
    return _has_probe(wake_check, clock_only) and not human_request and kind in AGENT_OWNED_KINDS


def _refuse_untimed_probe(kind: Optional[str], wake_check: Optional[str], clock_only: bool) -> None:
    """A wake_check / clock_only only means something on a park. On a block that lands
    ``blocked`` (a human ask, an assignment, a dependency on an open parent) it would be dropped
    and nothing would run it, so refuse instead. [daveey]"""
    if not _has_probe(wake_check, clock_only):
        return
    what = "wake_check" if wake_check and str(wake_check).strip() else "clock_only"
    raise ValueError(
        f"refused: {what} only applies to a timed park (kind='time', a SCHEDULED_UNTIL=<ISO8601> "
        f"reason, or an agent-owned block with no human_request). This block (kind={kind!r}) would "
        f"land '{'todo' if kind == 'dependency' else 'blocked'}' and drop the {what}, so nothing "
        "would run it. Use kind='time' (with its wake reason in awaiting) to wait on it; for a human ask, put it in human_request.resolved_check.")


@publication_guard
def block_task(
    conn: sqlite3.Connection, task_id: str, *, reason: Optional[str] = None,
    kind: Optional[str] = None, expected_run_id: Optional[int] = None,
    blocker_id: Optional[str] = None, wake_check: Optional[str] = None,
    check_every: Optional[int] = None, clock_only: bool = False,
    human_request: Optional[dict] = None, awaiting: Any = None, clear_blocker: bool = False,
    comment: Optional[tuple[str, str]] = None, goal_rejection=None,
    blocker_provider: Optional[str] = None, blocker_model: Optional[str] = None,
    blocker_lane: Optional[str] = None, actor: Optional[str] = None,
) -> bool:
    """``running``/``ready`` -> ``blocked`` (or ``todo`` / ``triage``, see
    :func:`kanban_block_routing.route_block`). A wait on time -- ``kind='time'``, a ``dependency``
    with no open parent, or a ``SCHEDULED_UNTIL=`` reason -- parks ``scheduled``
    with a wake time instead (``kanban_timed_park``). ``transient`` still counts
    toward the loop breaker. True on any transition. ``blocker_id`` names a
    shared external blocker (``kanban_shared_blockers``), stored on the closed
    run's metadata and the event payload. A blocker never cancels the timed park,
    and a blocker park with no human ask and no open parent is given a wake
    (``kanban_timed_park.needs_blocker_wake``): every parked card keeps a way back.
    ``clear_blocker`` records on the run that the caller saw the card's shared
    blocker gone (the only way a park without the id clears it). [daveey]

    An already-``blocked`` card that the failure breaker parked UNTYPED
    (``block_kind IS NULL``, no live run) is classified in place when *kind*
    is supplied: ``block_kind``/``block_recurrences`` are set and a ``blocked``
    audit event is appended, while status, failure evidence and the terminal
    runs stay exactly as the breaker left them. A typed block, a card with a
    live run, or a kind-less call on a blocked card are still refused.
    ``assignment`` is agent-owned: a capable configured supervisor receives a
    durable repair card and the original waits in ``todo`` on that dependency
    (including explicit classification of a breaker-parked card).

    ``comment`` (``(author, body)``) is written in the same txn, before the transition event,
    and only when the block lands (``kanban_reason_comment``).
    """
    from hermes_cli.kanban_ask_resolved import filing_fields, store_request, sync_row
    from hermes_cli.kanban_human_request import reject_timed_request
    from hermes_cli.kanban_block_routing import clear_meta, validate_block_request
    from hermes_cli.kanban_goal_recovery import record_block
    from hermes_cli.kanban_reason_comment import reason_comment
    human_request = validate_block_request(human_request, kind)
    pin = _park_pin.plan(conn, task_id, reason)  # git reads stay outside the write txn
    with write_txn(conn), reason_comment(conn, task_id, comment) as noted:
        if not record_block(conn, task_id, goal_rejection, reason, expected_run_id, kind, human_request):
            return False
        cur_row = conn.execute("SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if cur_row is None:
            return False
        _park_pin.apply(conn, task_id, pin)  # unpushed state keeps a pooled card on this worker (same run only)
        from hermes_cli.kanban_dependency_validation import validate_dependency_block
        validate_dependency_block(conn, task_id, kind)
        from hermes_cli import kanban_assignment_routing as assignment_routing
        from hermes_cli.kanban_blocker_metadata import blocker_metadata
        run_meta = blocker_metadata(conn, task_id, reason=reason, metadata=clear_meta(clear_blocker),
                                    blocker_id=blocker_id, blocker_provider=blocker_provider,
                                    blocker_model=blocker_model, blocker_lane=blocker_lane)
        # The breaker (``_record_task_failure``) parks cards ``blocked`` with no
        # ``block_kind`` and no ``blocked`` event -- the policy is the
        # supervisor's, not the kernel's -- but the transition guard below only
        # matches running/ready, so that policy could never be attached later
        # (#117363). Classify in place; never re-type or flap status. A caller
        # asserting run ownership (``expected_run_id``) cannot own a parked
        # card -- its run is over -- so it is refused like any stale worker.
        if cur_row["status"] == "blocked":
            if kind is None or expected_run_id is not None or _row_get(cur_row, "block_kind") is not None:
                return False

            # [daveey] Park untyped clock waits as scheduled too, not blocked + kind=time;
            # otherwise Desktop lists them under "For you" with an unblock button.
            from hermes_cli import kanban_timed_park as ktp
            from hermes_cli.kanban_shared_blockers import normalize_blocker_id
            blocker_id = normalize_blocker_id(blocker_id)
            satisfied = _parents_satisfied(conn, task_id)
            mode = ktp.park_mode(kind, reason, parents_satisfied=satisfied, blocker_id=blocker_id,
                                 human_request=human_request, wake_check=wake_check, clock_only=clock_only)
            if mode is None and _probe_parks(kind, wake_check, clock_only, human_request):
                mode = "timed"
            if mode is not None:
                reject_timed_request(human_request)
                return noted.keep(ktp.park(
                    conn, task_id, reason=reason, requested_kind=kind, source_status="ready",
                    from_untyped_block=True, wake_check=wake_check,
                    check_every=check_every, clock_only=clock_only, awaiting=awaiting,
                    blocker_id=blocker_id, blocker_wake=mode == "blocker",
                    run_metadata=run_meta))
            _refuse_untimed_probe(kind, wake_check, clock_only)
            requested_kind, rekind_reason = kind, None
            if kind == "assignment":
                kind, rekind_reason = assignment_routing.classify(get_task(conn, task_id), kind, reason, human_request=human_request)

            classified = conn.execute(
                "UPDATE tasks SET block_kind = ?, block_recurrences = 1, human_request = ? "
                "WHERE id = ? AND status = 'blocked' AND block_kind IS NULL "
                "AND current_run_id IS NULL",
                (kind, json.dumps(human_request) if human_request else None, task_id),
            ).rowcount
            if classified != 1:
                return False
            sync_row(conn, task_id)
            payload = {"kind": kind, "reason": reason, "classified_in_place": True,
                       "human_request": human_request, **filing_fields(conn, human_request), **run_meta, **({"actor": actor} if actor else {})}
            if requested_kind == "assignment":
                payload.update(requested_kind=requested_kind, rekind_reason=rekind_reason)
            if kind == "assignment":
                assignment_routing.route(conn, get_task(conn, task_id), payload)
            run_id = (_end_or_synthesize_run(conn, task_id, outcome="blocked", status="blocked",
                                            summary=reason, metadata=run_meta, synthesize=True)
                      if run_meta else None)
            _append_event(conn, task_id, "blocked", payload, run_id=run_id)
            return noted.keep(True)
        source_status = _retry_status_for_run(conn, task_id) if cur_row["status"] == "running" else cur_row["status"]
        requested_kind, rekind_reason = kind, None
        from hermes_cli.kanban_block_routing import classify_stop
        kind, rekind_reason, exhausted_budget = classify_stop(conn, task_id, kind, reason, blocker_id, human_request=human_request)
        no_open_parent = _parents_satisfied(conn, task_id)
        from hermes_cli import kanban_timed_park as ktp
        from hermes_cli.kanban_shared_blockers import normalize_blocker_id
        blocker_id = normalize_blocker_id(blocker_id)
        # A blocker never cancels the timed park, and a blocker park with no wake of its own
        # wakes on the probe cadence (``kanban_timed_park.park_mode``). An agent-owned block
        # that brings a wake_check / clock_only parks on that probe. [daveey]
        mode = None if exhausted_budget else ktp.park_mode(
            kind, reason, parents_satisfied=no_open_parent, blocker_id=blocker_id,
            human_request=human_request, wake_check=wake_check, clock_only=clock_only)
        if (mode is None and not exhausted_budget
                and _probe_parks(requested_kind, wake_check, clock_only, human_request)):
            mode = "timed"
        if mode is not None:
            reject_timed_request(human_request)
            return noted.keep(ktp.park(
                conn, task_id, reason=reason, requested_kind=kind,
                source_status=source_status, expected_run_id=expected_run_id,
                wake_check=wake_check, check_every=check_every, clock_only=clock_only, awaiting=awaiting,
                blocker_id=blocker_id, blocker_wake=mode == "blocker",
                run_metadata=run_meta))
        _refuse_untimed_probe(kind, wake_check, clock_only)
        # Parent-less ``dependency`` behind a shared blocker with a human ask: sticky ``needs_input``.
        if kind == "dependency" and no_open_parent:
            kind = "needs_input"
            rekind_reason = "no_open_parent"
        from hermes_cli.kanban_block_routing import (
            latest_block_reason, latest_run_blocker, route_block,
        )
        new_status, event_kind, set_sql, params, payload = route_block(
            kind, reason, source_status, prev_kind=_row_get(cur_row, "block_kind"),
            prev_recurrences=int(_row_get(cur_row, "block_recurrences") or 0),
            prev_reason=latest_block_reason(conn, task_id),
            # A dependency re-kinded to needs_input is an impossible wait, not a
            # human question: its rewording is no progress.
            compare_reason=requested_kind == "needs_input",
        )
        if exhausted_budget:
            new_status, event_kind = "triage", "blocked"
        if rekind_reason:
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
        if blocker_id:
            payload["blocker_id"] = blocker_id
            if (new_status == "triage" and _row_get(cur_row, "block_kind") == payload.get("kind") == kind
                    and latest_run_blocker(conn, task_id) == blocker_id):
                # A probe re-asserting the shared blocker it was parked behind is the
                # dispatcher's bounded probe cadence, not an unblock loop: keep the
                # recurrence count where it was and stay ``blocked``. [daveey]
                prev = max(int(_row_get(cur_row, "block_recurrences") or 0), 1)
                new_status, event_kind = "blocked", "blocked"
                params = (kind, prev)
                payload["recurrences"] = prev
                payload.pop("limit", None)
                payload["shared_blocker_probe"] = True
        from hermes_cli.kanban_dependency_review import apply_block_update
        if not apply_block_update(
            conn, task_id, expected_run_id, kind, new_status, set_sql, params, payload,
        ):
            return False
        store_request(conn, task_id, human_request)
        payload.update(status=new_status, human_request=human_request, **({"actor": actor} if actor else {}), **(filing_fields(conn, human_request) if new_status == "blocked" else {}))  # status: where it landed, read by the sync heal [daveey]
        from hermes_cli.kanban_shared_blockers import CLEAR_FLAG
        payload.update(run_meta)
        if clear_blocker:
            payload[CLEAR_FLAG] = True
        run_id = _end_or_synthesize_run(
            conn, task_id, outcome="blocked", status="blocked", summary=reason,
            metadata=run_meta or None,
            synthesize=bool(reason or run_meta),
        )
        if kind == "assignment":
            payload["requested_kind"] = requested_kind
            payload["rekind_reason"] = rekind_reason
            assignment_routing.route(conn, get_task(conn, task_id), payload)
        _append_event(conn, task_id, event_kind, payload, run_id=run_id)
        noted.keep()
        blocked_task = get_task(conn, task_id)
    _fire_task_hook("kanban_task_blocked", blocked_task, task_id, run_id, reason=reason)
    return True


from hermes_cli.kanban_review_model import redact_review_value


@publication_guard
def request_review(
    conn: sqlite3.Connection, task_id: str, *, summary: Optional[str] = None,
    metadata: Optional[dict] = None, reviewer: Optional[str] = None,
    expected_run_id: Optional[int] = None, force: bool = False, with_reason: bool = False,
):
    """``running``/``ready`` -> ``review``; never touches block recurrence accounting.

    Implementer and reviewer are recorded on the event so requested changes
    route back to the right profile; ``reviewer`` reassigns the task, and on
    re-review defaults to the latest ``changes_requested`` provenance. A live
    claim is only cleared with proof of ownership (``expected_run_id``) or
    ``force=True``. Returns ``bool``, or ``(ok, reason)`` with ``with_reason``.

    ``metadata["artifacts"]`` names the handoff's deliverable
    files; a review handoff is the last implementer transition, and the
    *reviewer's* completion is what cleans the managed scratch workspace up, so
    the files are staged into the task's durable attachments dir here and the
    staged paths ride the ``review_requested`` payload for the notifier to
    upload. A declared artifact that cannot be preserved raises
    :class:`ArtifactPreservationError`, rolling the whole transition back: the
    task stays ``running`` and retryable, with no attachments and no event.
    """

    def _ret(ok: bool, reason: Optional[str] = None):
        return (ok, reason) if with_reason else ok

    summary = redact_review_value(summary)
    metadata = redact_review_value(metadata)
    from hermes_cli.kanban_review_model import ReviewModelUnavailable, validate_review_override
    try:
        validate_review_override(metadata or {})
    except ReviewModelUnavailable as exc:
        return _ret(False, str(exc))
    # Declared (metadata["artifacts"]) and prose-referenced files
    # must be durable BEFORE anything can clean the scratch workspace up: for a
    # review-bound card the reviewer's completion is the cleanup trigger.
    metadata = _merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=None)
    now = int(time.time())
    # Staged copies live outside the txn: a rollback after staging must not
    # leave orphans that make the retry stage ``name_1.ext`` beside them.
    staged_copies: list[Path] = []
    try:
        with _prestaged(conn, task_id, metadata), write_txn(conn):
            if not _parents_satisfied(conn, task_id):
                return _ret(False, "parent dependencies are not satisfied")
            trow = conn.execute(
                "SELECT assignee, status, claim_lock, current_run_id, worker_pid, "
                "worker_started_at FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if trow is None:
                return _ret(False, "task not found")
            # Refuse to clear a live worker's claim without proof of ownership
            # (expected_run_id) or an explicit human override (force=True);
            # the same fence as complete_task (_claim_is_live).
            if expected_run_id is None and not force and _claim_is_live(trow):
                return _ret(
                    False, "task is running under a live claim; pass expected_run_id "
                    "(worker ownership) or force=True (explicit operator "
                    "override) instead of clearing the live run's claim",
                )
            if reviewer is None:
                reviewer = _prior_reviewer(conn, task_id)
                if reviewer is False:
                    return _ret(
                        False, "re-review has no durable reviewer provenance (the "
                        "latest changes_requested event is missing or "
                        "malformed); pass reviewer= explicitly",
                    )
            reviewer = _canonical_assignee(reviewer)
            if trow["status"] not in ("running", "ready") or (
                expected_run_id is not None and trow["current_run_id"] != int(expected_run_id)
            ):
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            if reviewer is not None:
                from hermes_cli.kanban_capabilities import validate_assignment

                task = get_task(conn, task_id)
                validate_assignment(reviewer, requires=task.requires,
                                    workspace_kind=task.workspace_kind, workspace_path=task.workspace_path, skills=task.skills)
            # The actor is the run that did the work. ``assignee`` is the actor
            # only while a worker holds the card; on a never-claimed card it is
            # whoever the operator assigned -- possibly the reviewer itself,
            # which is what ``kanban create --assignee <reviewer>`` followed by
            # ``request-review`` produces. Recording the reviewer as its own
            # implementer is worse than recording nothing: request_changes()
            # routes on this field, and it already refuses a handoff that
            # carries no implementer provenance.
            implementer = None
            if trow["current_run_id"] is not None:
                arow = conn.execute(
                    "SELECT profile FROM task_runs WHERE id = ?",
                    (trow["current_run_id"],),
                ).fetchone()
                implementer = arow["profile"] if arow else None
            if implementer is None and trow["assignee"] != reviewer:
                implementer = trow["assignee"]
            assignee_sql = ", assignee = ?" if reviewer is not None else ""
            run_guard = "" if expected_run_id is None else " AND current_run_id = ?"
            params: tuple[Any, ...] = (
                *(() if reviewer is None else (reviewer,)), task_id,
                *(() if expected_run_id is None else (int(expected_run_id),)),
            )
            cur = _sr.with_source_receipt(conn, task_id, lambda: conn.execute(
                """
                UPDATE tasks
                   SET status        = 'review',
                       human_request = NULL,
                       claim_lock    = NULL,
                       claim_expires = NULL,
                       worker_pid    = NULL
                """ + assignee_sql + """
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                """ + run_guard,
                params,
            ))
            if cur.rowcount != 1:
                return _ret(
                    False, "task is not in running/ready (or expected_run_id did not match the current run)",
                )
            if isinstance(metadata, dict):
                staged_copies = _stage_completion_artifacts(
                    conn, task_id, metadata, now, uploaded_by="kanban_request_review",
                )
            run_id = _end_or_synthesize_run(
                conn, task_id, outcome="review_requested", status="review",
                summary=summary, metadata=metadata, synthesize=bool(summary or metadata),
                profile=implementer,
            )
            payload: dict = {
                "summary": _first_line(summary, 400) or None,
                "implementer": implementer,
                "reviewer": reviewer,
            }
            staged = _cleaned_artifact_paths(metadata)
            if staged:
                payload["artifacts"] = staged
            _append_event(conn, task_id, "review_requested", payload, run_id=run_id)
    except Exception:
        if staged_copies:
            _discard_staged_copies(staged_copies, staged_copies[0].parent)
        raise
    return _ret(True)


def _prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.
    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed."""
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    changes_event = _latest_event(conn, task_id, "changes_requested", changes_run["id"])
    reviewer = _json_dict(_row_get(changes_event, "payload")).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def _nonblank_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


@source_receipt_preflight
def request_changes(
    conn: sqlite3.Connection, task_id: str, *, reason: str, expected_run_id: Optional[int] = None,
) -> tuple[bool, Optional[str]]:
    """Close an active reviewer run (claimed from ``review``) and hand the task
    back to the implementer from the latest ``review_requested`` event, parent
    gating reapplied. Returns ``(ok, implementer | reason)``."""
    reason = str(redact_review_value(reason or "")).strip()
    if not reason:
        return False, "reason is required"

    with write_txn(conn):
        task_row = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if task_row is None:
            return False, "task not found"
        current_run_id = task_row["current_run_id"]
        if task_row["status"] != "running" or current_run_id is None:
            return False, "task is not in an active review run"
        if expected_run_id is not None and int(current_run_id) != int(expected_run_id):
            return False, "run_id mismatch"

        claimed_event = _latest_event(conn, task_id, "claimed", current_run_id)
        claimed_payload = _json_dict(_row_get(claimed_event, "payload"))
        if claimed_payload.get("source_status") != "review":
            return False, "active run was not claimed from review"

        requested_event = _latest_event(conn, task_id, "review_requested")
        if requested_event is None:
            return False, "no prior review_requested event"
        implementer = _nonblank_str(_json_dict(requested_event["payload"]).get("implementer"))
        if implementer is None:
            return False, "review handoff has no valid implementer provenance"
        reviewer = _canonical_assignee(_nonblank_str(task_row["assignee"]))
        from hermes_cli.kanban_runs_on_capabilities import verdict_warning  # [daveey] never refuse a verdict
        capability_warning = verdict_warning(implementer, get_task(conn, task_id))

        new_status = _landing_status_after_parents(conn, task_id)
        # consecutive_failures deliberately PRESERVED: a review transition is
        # not evidence the pathology cleared; only complete_task resets it.
        cur = _sr.with_source_receipt(conn, task_id, lambda: conn.execute(
            """
            UPDATE tasks
               SET status = ?,
                   assignee = COALESCE(?, assignee),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL, worker_started_at = NULL
             WHERE id = ? AND status = 'running' AND current_run_id = ?
            """,
            (new_status, implementer, task_id, int(current_run_id)),
        ))
        if cur.rowcount != 1:
            return False, "task changed during review handoff"
        run_id = _end_run(conn, task_id, outcome="changes_requested", status=new_status, summary=reason)
        _run_end_lease.note(conn, task_id, run_id)  # [daveey] free the Board lease after commit
        _append_event(
            conn,
            task_id,
            "changes_requested",
            {
                "reason": reason,
                "implementer": implementer,
                "reviewer": reviewer,
                "status": new_status,
                **capability_warning,
            },
            run_id=run_id,
        )
    return True, implementer


_PROMOTABLE_STATUSES = ("todo", "blocked", "triage")


from hermes_cli.kanban_db_promote import promote_task


def _reclaim_dangling_run(
    conn: sqlite3.Connection, task_id: str, *, statuses, now: int, note: str,
) -> None:
    """Close a leaked open run before a status flip so the invariant
    ``current_run_id IS NULL <=> run row terminal`` holds; no-op normally."""
    placeholders = ", ".join("?" for _ in statuses)
    stale = conn.execute(
        f"SELECT current_run_id FROM tasks WHERE id = ? AND status IN ({placeholders})",
        (task_id, *statuses),
    ).fetchone()
    if stale and stale["current_run_id"]:
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'reclaimed', outcome = 'reclaimed',
                   summary = COALESCE(summary, ?),
                   ended_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
             WHERE id = ? AND ended_at IS NULL
            """,
            (note, now, int(stale["current_run_id"])),
        )


def _landing_status_after_parents(conn: sqlite3.Connection, task_id: str) -> str:
    """``ready`` if every parent is terminal else ``todo`` — the re-gate shared by
    unblock/reopen so neither can spawn a child whose upstream is unfinished."""
    return "ready" if _parents_satisfied(conn, task_id) else "todo"


def unblock_task(conn: sqlite3.Connection, task_id: str, *, comment: ReasonComment = None,
                 event_extra: Optional[dict] = None,
                 precondition: Optional[Callable[[sqlite3.Connection], Optional[str]]] = None) -> bool:
    """``blocked``/``scheduled``/``triage`` -> its resumable phase (parent re-gated; ``review`` when
    that is where it left off), closing any leaked run first. ``triage`` is accepted so a card a block
    loop escalated can go back to work without the specifier/decomposer rewriting it. ``comment`` is
    written in the same txn and only when the unblock lands (``kanban_reason_comment``); ``event_extra``
    is merged into the ``unblocked`` event (audit provenance). ``precondition`` runs first inside the
    write txn; a returned refusal aborts with ``False`` and no change. A card held terminal is refused before both
    (``kanban_terminal_guard.unblock_held``: ``unblocked`` is a reopen). [daveey]"""
    from hermes_cli.kanban_reason_comment import reason_comment
    from hermes_cli.kanban_terminal_guard import unblock_held
    now = int(time.time())
    # ``held`` is decided in the write txn, before ``precondition`` and the comment savepoint (its rollback drops notes).
    with write_txn(conn):
        held = unblock_held(conn, task_id, event_extra=event_extra)
        with reason_comment(conn, task_id, None if held else comment) as noted:
            if held or precondition is not None and precondition(conn):
                return False
            resume_status = (_resume_status_from_events(conn, task_id)
                             if _task_status(conn, task_id) in ("blocked", "triage", "scheduled") else "ready")
            _reclaim_dangling_run(
                conn, task_id, statuses=("blocked", "scheduled", "triage"), now=now,
                note="invariant recovery on unblock",
            )
            # Re-gate on parent completion before restoring the source phase.
            landing_status = _landing_status_after_parents(conn, task_id)
            new_status = "review" if landing_status == "ready" and resume_status == "review" else landing_status
            # ``block_kind``/``block_recurrences`` deliberately survive the unblock:
            # resetting them is the amnesia that let cron-unblock <-> re-block loop
            # unbounded; only complete_task clears them. ``consecutive_failures``
            # (the dispatcher's spawn/crash counter) IS reset — a deliberate unblock
            # is a fresh start for the retry budget.
            cur = conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, "
                "consecutive_failures = 0, last_failure_error = NULL, human_request = NULL "
                "WHERE id = ? AND status IN ('blocked', 'scheduled', 'triage')", (new_status, task_id),
            )
            if cur.rowcount != 1:
                return False
            payload = ({"status": new_status, "resume_status": resume_status}
                       if new_status != "ready" or resume_status != "ready" else None)
            if event_extra:
                payload = {**(payload or {}), **event_extra}
            _append_event(conn, task_id, "unblocked", payload)
            return noted.keep(True)


def invalidate_descendants_for_parent_reopen(
    conn: sqlite3.Connection, task_id: str, *, author: str,
) -> dict[str, Any]:
    """THE done-reopen invalidation: every ``ready``/``review``/``running``/``done``
    descendant of a reopened ancestor is demoted to ``todo`` and re-gated.
    Every surface that reopens a done task (dashboard PATCH/drag) routes here.

    Composes under the caller's txn (``allow_nested=True``) so the flip and the
    retractions commit atomically. Each descendant gets a
    ``descendant_invalidated`` event, the legacy ``status`` event the live feed
    renders, and a comment naming the ancestor. Running descendants are closed
    ``reclaimed`` and their workers killed strictly post-commit (audit trail
    before death) — when composed, the CALLER must drain ``terminations``
    after its own commit. ``consecutive_failures`` resets (deliberate operator
    action), the opposite of :func:`reopen_review_task`.

    Returns ``{"invalidated": [{id, prior_status, new_status, resume_status}],
    "terminations": [(worker_pid, claim_lock, worker_started_at)]}``.
    """
    from hermes_cli.kanban_priority import bound_reopened

    caller_owns_txn = bool(conn.in_transaction)
    now = int(time.time())
    invalidated: list[dict[str, Any]] = []
    terminations: list[tuple[Optional[int], Optional[str], Optional[int]]] = []
    with write_txn(conn, allow_nested=True):
        bound_reopened(conn, task_id)
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT child_id FROM task_links WHERE parent_id = ?
                UNION
                SELECT l.child_id
                FROM task_links l
                JOIN descendants d ON d.id = l.parent_id
            )
            SELECT t.id, t.status, t.current_run_id, t.worker_pid, t.claim_lock, t.worker_started_at
            FROM descendants d
            JOIN tasks t ON t.id = d.id
            ORDER BY t.id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            previous_status = row["status"]
            if previous_status not in {"ready", "review", "running", "done"}:
                continue
            resume_status = "ready"
            run_id = None
            if previous_status == "review":
                resume_status = "review"
            elif previous_status == "running":
                resume_status = _retry_status_for_run(conn, row["id"], row["current_run_id"])
                terminations.append((row["worker_pid"], row["claim_lock"], row["worker_started_at"]))
                run_id = _end_run(conn, row["id"], outcome="reclaimed", status="todo",
                                  summary=f"ancestor {task_id} reopened")
                _run_end_lease.note(conn, row["id"], run_id)  # [daveey] free the Board lease after commit
            # consecutive_failures = 0: deliberate operator reset — see
            # docstring for why this diverges from reopen_review_task.
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "current_run_id = NULL, consecutive_failures = 0, human_request = NULL WHERE id = ?", (row["id"],),
            )
            bound_reopened(conn, row["id"])
            entry = {
                "id": row["id"], "prior_status": previous_status,
                "new_status": "todo", "resume_status": resume_status,
            }
            _append_event(
                conn, row["id"], "descendant_invalidated",
                {"ancestor": task_id, **{k: v for k, v in entry.items() if k != "id"}},
                run_id=run_id,
            )
            # Legacy 'status' event so existing live-feed consumers still see
            # the move without learning the new event kind.
            _append_event(
                conn, row["id"], "status",
                {
                    "status": "todo", "reason": "ancestor_reopened", "parent": task_id,
                    "previous_status": previous_status, "resume_status": resume_status,
                },
                run_id=run_id,
            )
            _insert_comment(
                conn, row["id"], author, f"Invalidated: ancestor {task_id} was reopened; "
                f"retracted from '{previous_status}' to 'todo' "
                f"(will resume via '{resume_status}').", now,
            )
            invalidated.append(entry)
    if not caller_owns_txn:
        # Standalone: committed above, audit trail durable, safe to kill now.
        # Composed calls leave this to the caller post-commit.
        for pid, claim_lock, started_at in terminations:
            _terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    return {"invalidated": invalidated, "terminations": terminations}


# Events after a ``block_loop_detected`` that count as new human input on a
# triage card: a comment, an edit, or a manual status move. Worker comments land
# before the escalation (same txn window), so anything later came from someone else.
_HUMAN_INPUT_EVENT_KINDS = ("commented", "edited", "status")


def awaiting_human_after_block_loop(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when a triage card was escalated by ``block_loop_detected`` and has had
    no human input (comment/edit/status move) since. The auto-decomposer must leave
    such cards alone: re-specifying them re-promotes the same blocked work and the
    worker just re-blocks, burning a run every couple of minutes.
    """
    row = conn.execute(
        "SELECT MAX(id) AS eid FROM task_events WHERE task_id = ? AND kind = 'block_loop_detected'",
        (task_id,),
    ).fetchone()
    loop_eid = row[0] if row is not None else None
    if loop_eid is None:
        return False
    kinds = ", ".join("?" for _ in _HUMAN_INPUT_EVENT_KINDS)
    later = conn.execute(
        f"SELECT 1 FROM task_events WHERE task_id = ? AND id > ? AND kind IN ({kinds}) LIMIT 1",
        (task_id, loop_eid, *_HUMAN_INPUT_EVENT_KINDS),
    ).fetchone()
    return later is None


def decomposition_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Why *task_id* must not be specified/decomposed by the aux LLM, or None.

    Only never-started cards are candidates. A card that has run (``started_at``
    set or any ``task_runs`` row) carries shipped work and handoffs the LLM does
    not see; re-decomposing it from its body re-implements the shipped feature.
    A ``block_loop_detected`` escalation is a wait for a human, not a request
    for a new spec. Either way the exit is ``hermes kanban promote`` / ``kanban_unblock`` (triage -> ready). [daveey]
    """
    from hermes_cli.kanban_lanes import gate_refusal
    row = conn.execute("SELECT started_at FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or (held := (_containment.refusal(conn, task_id) or gate_refusal(conn, task_id))):
        return held if row is not None else None
    ran = row["started_at"] is not None or conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id = ? LIMIT 1", (task_id,),
    ).fetchone() is not None
    if ran:
        return ("task has already run; decomposing it would redo its shipped work. "
                f"Return it to work with `hermes kanban promote {task_id}` or kanban_unblock")
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'block_loop_detected' LIMIT 1",
        (task_id,),
    ).fetchone() is not None:
        return ("task was escalated to triage by block-loop detection and is waiting for a human; "
                f"return it to work with `hermes kanban promote {task_id}` or kanban_unblock")
    return None


def specify_triage_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, assignee: Optional[str] = None, author: Optional[str] = None,
) -> bool:
    """Update title/body/assignee (when given) and move ``triage -> todo`` in one txn; False when not in triage.
    Lands in ``todo`` (not ``ready``) so parent gating still applies; the audit comment only when a field changed.
    """
    from hermes_cli.kanban_model_neutral import validate_text
    validate_text(title, body)
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    from hermes_cli.kanban_gate_only import create_assignee
    assignee = create_assignee(_canonical_assignee(assignee))  # promotion is not the gate: gate-only -> queue [daveey]
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        from hermes_cli.kanban_capabilities import validate_assignment
        task = get_task(conn, task_id)
        validate_assignment(assignee or task.assignee, requires=task.requires,
                            workspace_kind=task.workspace_kind,
                            workspace_path=task.workspace_path, skills=task.skills)
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'", tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Not add_comment (own txn + 'commented' event); 'specified' below records it.
            _insert_comment(
                conn, task_id, author.strip(),
                "Specified — updated " + ", ".join(changed_fields) + " and promoted to todo.",
                int(time.time()),
            )
        _append_event(
            conn, task_id, "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Own IMMEDIATE txn (outside the one above): a parent-free specified task
    # flips to 'ready' now instead of idling until the next tick.
    recompute_ready(conn)
    return True


def _delete_task_relations(conn: sqlite3.Connection, task_id: str) -> None:
    """Delete every row referencing ``task_id`` (schema has no ON DELETE CASCADE)."""
    conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
    from hermes_cli.kanban_containment_connections import released_history_delete
    for table in ("task_comments", "task_events", "task_runs", "kanban_notify_subs"):
        if table == "task_events":
            with released_history_delete(conn, task_id):
                conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            continue
        conn.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
    with contextlib.suppress(sqlite3.OperationalError):  # board predating condition wakes
        conn.execute("DELETE FROM task_wake_checks WHERE task_id = ?", (task_id,))


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete an ARCHIVED task (+ related rows); anything else must be
    archived first so data loss takes two deliberate actions."""
    with write_txn(conn):
        if _task_status(conn, task_id) != "archived":
            return False
        _delete_task_relations(conn, task_id)
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and its related rows in one txn; False when not found."""
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        _delete_task_relations(conn, task_id)
    recompute_ready(conn)
    return True


# --- Worker context builder (what a spawned worker sees) ---

def build_worker_context(conn: sqlite3.Connection, task_id: str, *, recovery_tail: bool = True) -> str:
    """Everything a worker should read about its task: header, body, attachments, prior attempts,
    done-parent handoffs, the assignee's recent work, comments. Lists are tail-capped and fields char-capped
    (``_CTX_MAX_*``) so the prompt stays bounded. ``recovery_tail=False`` omits the transcript tail (a run
    resumed after an owner restart carries the lost run's whole conversation instead). [daveey]"""
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")
    from hermes_cli.kanban_db_context import (
        _ctx_attachments, _ctx_comments, _ctx_header, _ctx_parent_results, _ctx_prior_attempts,
        _ctx_role_history,
    )
    # One clock reading so every relative age in this rendering agrees.
    now = int(time.time())
    lines: list[str] = []
    _ctx_header(lines, task, conn=conn)
    from hermes_cli.kanban_containment_authority import render_context
    authority_notice = render_context(conn, task_id)
    if authority_notice:
        lines.append(authority_notice)
    _ctx_standing(lines, task, list_comments(conn, task_id))  # [daveey]
    from hermes_cli.kanban_continuation import render_checkpoint_context
    checkpoint = render_checkpoint_context(conn, task_id)
    if checkpoint:
        lines.append(checkpoint)
    from hermes_cli.kanban_wake_check import render_wake_context
    lines += [w for w in (render_wake_context(conn, task_id),) if w]  # condition wake [daveey]
    from hermes_cli.kanban_shared_blockers import render_blocker_context
    try:
        blockers = render_blocker_context(conn, task_id)
    except Exception:  # advisory only; never break context assembly
        _log.debug("shared blocker context failed for %s", task_id, exc_info=True)
        blockers = ""
    if blockers:
        lines.append(blockers)
    _ctx_attachments(lines, list_attachments(conn, task_id))
    _ctx_prior_attempts(lines, conn, task_id, now)
    _ctx_parent_results(lines, conn, task_id, now)
    _ctx_role_history(lines, conn, task, now)
    _ctx_comments(lines, conn, task_id, now)
    from hermes_cli.kanban_worker_recovery import build_recovery_context
    return "\n".join(lines).rstrip() + "\n" + (build_recovery_context(conn, task_id) if recovery_tail else "")


def _ctx_standing(lines: list[str], task: Task, comments: list[Comment]) -> None:
    """Every ``standing:`` line in the body or ANY comment, word for word, ahead of everything else. [daveey]

    The comment thread is tail-capped (``_CTX_MAX_COMMENTS``), so a standing "don't wait" written early on a
    busy card would otherwise drop out of the worker's prompt."""
    from hermes_cli.kanban_db_context import _ctx_standing as render_standing
    render_standing(lines, task, comments)


# --- Stats + SLA helpers ---

def board_stats(conn: sqlite3.Connection) -> dict:
    """Public board summary entry point."""
    from hermes_cli.kanban_db_stats import board_stats as summarize
    return summarize(conn)



def _counts_by_assignee(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """``{assignee: {status: n}}`` over non-archived tasks."""
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])
    return counts


def _to_epoch(val) -> Optional[int]:
    """Epoch seconds from int/float/numeric string/ISO-8601; None for empty/invalid."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c, _s, _co = task.created_at, task.started_at, task.completed_at  # from_row coerced
    return {
        "created_age_seconds": now - _c if _c is not None else None,
        "started_age_seconds": now - _s if _s is not None else None,
        "time_to_complete_seconds": _co - (_s or _c) if _co is not None else None,
    }


# --- Retention + garbage collection ---

def _retention_seconds(older_than_seconds: int) -> int:
    """Normalise a gc retention window, rejecting negatives.

    Shared by both gc sweeps: a negative window puts the cutoff in the future,
    so "older than cutoff" would match every row / file instead of none —
    refuse before any sweep runs.
    """
    older_than_seconds = int(older_than_seconds)
    if older_than_seconds < 0:
        raise ValueError(
            f"older_than_seconds must be >= 0, got {older_than_seconds!r}: "
            "a negative retention selects everything."
        )
    return older_than_seconds


def gc_events(conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600) -> int:
    """Prune old done/archived events, retaining decomposition identity and carried origin intents until task deletion.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--event-retention-days 0`` to "disabled" before calling this.
    """
    from hermes_cli.kanban_containment_terminal import parent_status_sql
    cutoff = int(time.time()) - _retention_seconds(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND kind NOT IN ('decomposed', 'origin_intent_recorded', "
            "'contained', 'containment_released', 'containment_closed') "
            f"AND task_id IN (SELECT id FROM tasks WHERE {parent_status_sql(conn)} IN ('done', 'archived'))", (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(*, older_than_seconds: int = 30 * 24 * 3600, board: Optional[str] = None) -> int:
    """Delete worker log files older than the cutoff on one board; returns the count.

    ``older_than_seconds=0`` means everything older than now; the CLI maps
    ``--log-retention-days 0`` to "disabled" before calling this.
    """
    older_than_seconds = _retention_seconds(older_than_seconds)
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    return unlink_files_older_than(log_dir, "*", older_than_seconds)


# --- Worker log accessor ---

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Worker log path (may not exist). The dispatcher always passes ``board``
    explicitly to avoid resolution ambiguity."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None, board: Optional[str] = None,
) -> Optional[str]:
    """Worker log text (last ``tail_bytes`` when set); None when the file is missing."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8-sig", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip the partial first line unless the window has no newline
                # at all (readline() would eat everything).
                probe = f.tell()
                if not f.readline().endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return None


# --- Assignee enumeration (known profiles + per-profile board stats) ---

def list_profiles_on_disk() -> list[str]:
    """Profiles with a ``config.yaml`` plus the implicit ``default``; reads paths
    directly to avoid importing ``hermes_cli.profiles`` at startup."""
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")
    if profiles_dir.is_dir():
        try:
            names.update(e.name for e in profiles_dir.iterdir() if e.is_dir() and (e / "config.yaml").is_file())  # config-reader: ok — profile directory discovery marker
        except OSError:
            pass
    return sorted(names)


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """``{"name", "on_disk", "counts"}`` for every on-disk profile or task
    assignee, so a fresh profile appears in pickers before it has a task."""
    on_disk = set(list_profiles_on_disk())
    counts = _counts_by_assignee(conn)
    return [
        {"name": name, "on_disk": name in on_disk, "counts": counts.get(name, {})}
        for name in sorted(on_disk | set(counts))
    ]


# --- Runs (attempt history on a task) ---

def list_runs(
    conn: sqlite3.Connection, task_id: str, *, include_active: bool = True,
    state_type: Optional[str] = None, state_name: Optional[str] = None,
) -> list[Run]:
    """Runs in start order; ``include_active=False`` = closed only; ``state_type``
    (``status``/``outcome``) + ``state_name`` filter together."""
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None and state_type not in ("status", "outcome"):
        raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Newest non-empty run summary, or None. Workers hand off via ``summary`` and
    leave ``tasks.result`` NULL, so views need this or a done task looks empty."""
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1", (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, str]:
    """``{task_id: newest non-empty run summary}`` in one query (window function,
    SQLite >= 3.25); tasks without a summary are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}


def current_run_started_ats(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, int]:
    """``{task_id: started_at of the run ``tasks.current_run_id`` points at}``
    in one query; tasks with no active run (NULL or dangling pointer) are omitted."""
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT t.id AS task_id, r.started_at AS started_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        f"WHERE t.id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r["task_id"]: r["started_at"] for r in rows}


# --- Split modules (imported at the tail: they import this module as ``_kb``) ---
from hermes_cli.kanban_db_comment_ids import (
    add_comment, agent_comment_ids, comment_id_before_run_start, delivered_comment_ids, own_comment_ids, writer_provenance)
from hermes_cli.kanban_db_archive import archive_task
from hermes_cli.kanban_db_list import list_task_rows, list_tasks
from hermes_cli.kanban_db_schedule import schedule_task
from hermes_cli.kanban_db_review_reopen import reopen_review_task
from hermes_cli.kanban_db_edit import PriorityLowerRefused, PriorityRaisePacedRefused, edit_task, paced_raise_refusal
from hermes_cli.kanban_reason_comment import Comment as ReasonComment, _awaiting_human_answer
from hermes_cli.kanban_db_notify import _inherit_notify_subs, _owning_sub_clause
from hermes_cli.kanban_db_boards import (
    _default_board_display_name,
    _dir_holds_board,
    board_metadata_path,
    create_board,
    list_boards,
    read_board_metadata,
    remove_board,
    write_board_metadata,
)
from hermes_cli.kanban_db_connect import (
    _INITIALIZED_PATHS,
    init_db,
    write_txn,
)
from hermes_cli.kanban_db_workspace import (
    _cleanup_workspace,
    _is_managed_scratch_path,
    _managed_scratch_path_info,
    _scratch_workspace,
)
from hermes_cli.kanban_db_stale_claims import _stale_claim_kept
from hermes_cli.kanban_db_heartbeat import heartbeat_claim
from hermes_cli.kanban_db_dispatch import (
    DEFAULT_FAILURE_LIMIT,
    DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
    DispatchResult,
    _clear_failure_counter,
    _defer_reclaim_for_live_worker,
    _pid_alive,
    _record_task_failure,
    _terminate_reclaimed_worker,
    _worker_alive,
    _worker_survived_termination,
    _worker_terminal_timeout_env,
)
