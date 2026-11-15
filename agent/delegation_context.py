"""Context-local state for delegate_task child execution.

A Hermes process may itself be a Kanban dispatcher worker with HERMES_KANBAN_* in
os.environ. In-process delegate_task children and cron jobs fired via
``cronjob(action="run")`` are NOT dispatcher-owned, so identity gates must fail
closed for them without mutating the process-global environment.
"""
from __future__ import annotations

import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator, Mapping, MutableMapping, overload

_DELEGATED_CHILD_CONTEXT: ContextVar[bool] = ContextVar("hermes_delegated_child_context", default=False)
# One opaque id per entered delegate_task child, outermost first. delegate_task children share
# the parent's terminal environment (the #16177 shared-container intent), so the terminal keys a
# per-child bash snapshot overlay on these ids: a child's ``export`` persists for that child's
# later commands but never reaches the parent's (or a sibling's) shell. [daveey]
_DELEGATED_CHILD_SCOPE: ContextVar[tuple[str, ...]] = ContextVar("hermes_delegated_child_scope", default=())
# Any in-process execution that is NOT the dispatcher-owned worker (cron jobs). Kept separate
# so delegate_task-specific behaviour (subprocess env scrubbing, its error strings) is unchanged.
_NON_DISPATCHER_OWNED_CONTEXT: ContextVar[bool] = ContextVar("hermes_non_dispatcher_owned_context", default=False)

DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"
# "<pid>:<start fingerprint>" of the agent process that first fenced the lineage. [daveey]
DELEGATED_CHILD_OWNER_ENV = "HERMES_DELEGATED_CHILD_OWNER"

_log = logging.getLogger(__name__)

KANBAN_ENV_KEYS: tuple[str, ...] = (
    "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_GOAL_MODE", "HERMES_KANBAN_GOAL_MAX_TURNS",
)
# [daveey] Informational only: the card a Kanban worker's tool children (terminal, background
# processes, execute_code, ...) run for. ``scrub_kanban_env`` removes ``HERMES_KANBAN_TASK`` from
# them so a nested ``hermes`` never acts as the worker, which also left shell tooling (gh-shim
# attribution, bb-land card notes, the compile cap) unable to tell it runs for a card. Nothing
# that decides worker identity, tools or board scope may read this name; use ``owned_kanban_task``.
KANBAN_WORKER_TASK_ENV = "HERMES_KANBAN_WORKER_TASK"

# --- Agent-scope env: what a top-level surface must never inherit. [daveey] -----------------
# A worker's terminal subprocesses carry DELEGATED_CHILD_ENV_MARKER by design
# (``delegated_child_subprocess_env``), plus the worker's session/kanban/cron/RPC identity.
# When one of them launched Hermes Desktop (a worker's ``hermes update`` reopening the app with
# ``open``), every chat became a fenced "delegate child" (Hermes.app pid 59331, 2026-10-21).
# A Desktop, ``serve``/``dashboard`` backend or gateway is never a worker, so each strips this
# family at launch. Mirrored in apps/desktop/electron/leaked-worker-env.ts (drift-tested).
#
# Presence of any marker means the env was inherited from a worker or delegate child. Without
# a marker nothing is stripped: a board pin or session var set alone is an operator choice.
AGENT_SCOPE_MARKER_KEYS: tuple[str, ...] = (
    DELEGATED_CHILD_ENV_MARKER,
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
)
AGENT_SCOPE_EXACT_KEYS: tuple[str, ...] = (
    DELEGATED_CHILD_ENV_MARKER,
    DELEGATED_CHILD_OWNER_ENV,
    "HERMES_SINGLE_QUERY_SESSION",
    "HERMES_SPAWN",
    "HERMES_SUPERVISED_CHILD",
    # The dispatcher pins TERMINAL_CWD to the worker's workspace; a Desktop that inherits it
    # anchors file tools and context files there (pid 59331 had .../workspaces/t_45d5a361).
    "TERMINAL_CWD",
    "HERMES_UI_SESSION_ID",  # gateway.session_context._VAR_MAP outside the HERMES_SESSION_ prefix
)
AGENT_SCOPE_PREFIXES: tuple[str, ...] = (
    "HERMES_KANBAN_",
    "HERMES_SESSION_",
    "HERMES_CRON_",
    "HERMES_PARENT_",
    "HERMES_RPC_",
    "HERMES_KERNEL_",
    "HERMES_BROWSER_CONTROL_",
)
# Operator tunables sharing those prefixes (HERMES_CRON_TIMEOUT, HERMES_SESSION_STALL_TIMEOUT,
# HERMES_CRON_MAX_PARALLEL, HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS...) are config, not identity.
AGENT_SCOPE_TUNABLE_RE = re.compile(r"(_TIMEOUT|_TIMEOUT_S|_POLL_S|_SECONDS|_MAX_PARALLEL)$")


def is_agent_scope_env_key(key: str) -> bool:
    """Whether *key* belongs to a worker/delegate/session identity (not an operator tunable)."""
    if key in AGENT_SCOPE_EXACT_KEYS:
        return True
    return key.startswith(AGENT_SCOPE_PREFIXES) and not AGENT_SCOPE_TUNABLE_RE.search(key)


def strip_agent_scope_env(
    env: MutableMapping[str, str], *, preserve_board_location: bool = False,
) -> list[str]:
    """Remove the agent-scope family from *env* in place when a worker marker is present.

    Returns the removed key names (empty when nothing leaked). Top-level launch
    scrubbing removes everything; ordinary lineage checks preserve explicit board
    locations so checking identity cannot silently redirect a caller's database.
    """
    if not any(env.get(key) for key in AGENT_SCOPE_MARKER_KEYS):
        return []
    keep = {"HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"} if preserve_board_location else set()
    removed = [key for key in list(env) if is_agent_scope_env_key(key) and key not in keep]
    for key in removed:
        env.pop(key, None)
    return removed


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    import uuid

    token = _DELEGATED_CHILD_CONTEXT.set(True)
    scope_token = _DELEGATED_CHILD_SCOPE.set((*_DELEGATED_CHILD_SCOPE.get(), uuid.uuid4().hex[:12]))
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_SCOPE.reset(scope_token)
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


def delegated_child_scope_chain() -> tuple[str, ...]:
    """Ids of the delegate_task children this code runs inside, outermost first (``()`` outside
    any child). Used to give each child its own terminal snapshot overlay. [daveey]"""
    return _DELEGATED_CHILD_SCOPE.get() if _DELEGATED_CHILD_CONTEXT.get() else ()


def enter_non_dispatcher_owned_context() -> Token[bool]:
    """Token form of :func:`non_dispatcher_owned_context` for long try/finally scopes."""
    return _NON_DISPATCHER_OWNED_CONTEXT.set(True)


def exit_non_dispatcher_owned_context(token: Token[bool]) -> None:
    """Restore the flag saved by :func:`enter_non_dispatcher_owned_context`."""
    _NON_DISPATCHER_OWNED_CONTEXT.reset(token)


@contextmanager
def non_dispatcher_owned_context() -> Iterator[None]:
    """Mark in-process execution that does NOT own the dispatcher's Kanban task; without it
    a cron agent run inside a worker is misread as that worker (kanban toolset force-added,
    ``kanban_complete`` defaulting to its task). ContextVar-scoped rather than clearing
    os.environ, which the worker's claim heartbeat and concurrent readers share."""
    token = enter_non_dispatcher_owned_context()
    try:
        yield
    finally:
        exit_non_dispatcher_owned_context(token)


def is_dispatcher_owned_worker_context() -> bool:
    """The single predicate every ``HERMES_KANBAN_*`` identity gate should use."""
    return not (is_delegated_child_process_context() or _NON_DISPATCHER_OWNED_CONTEXT.get())


def explicit_board_intent_is_pinned() -> bool:
    """Whether an explicit kanban ``board=`` argument must still resolve through
    the dispatcher-injected env pins (``HERMES_KANBAN_DB`` & friends) rather than
    its own board directory.

    True for in-process delegate children, descendants carrying
    :data:`DELEGATED_CHILD_ENV_MARKER`, and dispatched workers
    (``HERMES_KANBAN_TASK`` set). The pins are the "workers physically cannot
    see other boards" isolation, and :func:`kanban_path_is_fenced` checks the
    pinned path / fenced root — an explicit board that resolved elsewhere would
    also escape that fence. Outside these fences an explicit board is the
    caller's own intent and wins.
    """
    if _DELEGATED_CHILD_CONTEXT.get():
        return True
    if os.environ.get(DELEGATED_CHILD_ENV_MARKER):
        return True
    return bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip())


def explicit_board_resolves_inside_fence(path: os.PathLike[str] | str) -> bool:
    """Whether a spawned descendant's explicit board (``hermes kanban --board B`` run from a
    worker's terminal) may resolve to *path*, its own board file, instead of the inherited
    ``HERMES_KANBAN_DB`` pin. [daveey]

    Only for a process fenced by an inherited env marker alone: not an in-process delegate child
    and not the dispatcher-owned worker (``HERMES_KANBAN_TASK``), whose tools stay pinned. And
    only when *path* is itself fenced (under the lineage's board root), so writes there are still
    refused by ``kanban_db._assert_not_delegated_child_mutation``: the descendant gains reads of
    the board it named, never a write outside the fence. Without this a worker's
    ``hermes kanban --board runners show <id>`` silently read its own board and said "no such task".
    """
    if _DELEGATED_CHILD_CONTEXT.get() or (os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return False
    if not _live_env_marker():
        return False
    return kanban_path_is_fenced(path)


def owned_kanban_task() -> str:
    """The board task this execution OWNS: ``HERMES_KANBAN_TASK`` for the dispatcher-owned
    worker, ``""`` otherwise. Tool access is not worker identity — a profile can expose the
    kanban toolset interactively, and children/cron runs inherit the env var — so every
    reader that turns the task id into worker behaviour (guidance, stop nudge, terminal
    outcomes) goes through this one helper."""
    if not is_dispatcher_owned_worker_context():
        return ""
    return (os.environ.get("HERMES_KANBAN_TASK") or "").strip()


# --- Lineage binding for the env marker. [daveey] ---------------------------------------------
# An env var is inherited by EVERY descendant, including processes that leave the lineage: an app a
# worker reopens with ``open`` (Hermes.app pid 59331, 2026-10-21) or launches detached (BabyBear,
# ppid 1, 2026-10-22). Scrubbing at each launch site kept missing one, so the marker now names
# the agent process that fenced the lineage, and only counts in a process that still descends
# from that exact process incarnation. A marker with no owner (pre-binding) still fences.
_LIVE_TTL_S = 10.0
_live_cache: dict[tuple[int, str, str], tuple[float, bool]] = {}


def _start_fingerprint(pid: int) -> int | None:
    try:
        from gateway.status import _get_process_start_time
        return _get_process_start_time(pid)
    except Exception:
        return None


def _owner_token() -> str:
    pid = os.getpid()
    fp = _start_fingerprint(pid)
    return f"{pid}:{fp}" if fp is not None else str(pid)


def _parent_pid(pid: int) -> int | None:
    try:
        import psutil
        return int(psutil.Process(pid).ppid())
    except Exception:
        return None


def _descends_from(owner: int) -> bool:
    """Keep the fence unless a complete ancestry walk proves detachment.

    Missing psutil, access denial and a truncated walk are unknown, not evidence
    that this process left the owner's lineage.
    """
    current = os.getpid()
    for _ in range(64):
        if current == owner:
            return True
        parent = _parent_pid(current)
        if parent is None or parent == current:
            return True
        if parent == 0:
            return False
        current = parent
    return True


def _owner_is_live_ancestor(owner: str) -> bool:
    pid_s, _, fp_s = owner.partition(":")
    try:
        pid = int(pid_s)
    except ValueError:
        return True  # unreadable owner: fail closed (keep the fence)
    if fp_s:
        current = _start_fingerprint(pid)
        if current is None:
            # The fingerprint provider also returns None on lookup/import failure.
            # A complete parent walk can still prove detachment; absence cannot.
            return _descends_from(pid)
        try:
            from gateway.status import start_time_fingerprints_match
            if not start_time_fingerprints_match(int(fp_s), current):
                return False  # pid recycled by another process
        except (ImportError, ValueError):
            pass
    return _descends_from(pid)


def env_marker_is_live(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process's inherited delegate marker is real: present, and either unbound
    (legacy, fails closed) or bound to an owner process this process still descends from."""
    env = os.environ if environ is None else environ
    marker = env.get(DELEGATED_CHILD_ENV_MARKER, "")
    if not marker:
        return False
    owner = (env.get(DELEGATED_CHILD_OWNER_ENV) or "").strip()
    if not owner:
        return True
    key = (os.getpid(), marker, owner)
    now = time.monotonic()
    hit = _live_cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    live = _owner_is_live_ancestor(owner)
    _live_cache[key] = (now + _LIVE_TTL_S, live)
    return live


def _heal_leaked_marker() -> None:
    """Drop an orphaned marker (and the worker identity riding with it) from this process, so
    neither it nor anything it spawns is fenced as a delegate child of a lineage it left."""
    owner = os.environ.get(DELEGATED_CHILD_OWNER_ENV, "")
    removed = strip_agent_scope_env(os.environ, preserve_board_location=True)
    if removed:
        _log.warning("dropped leaked delegate-child env %s: pid %d does not descend from owner %s",
                     ", ".join(sorted(removed)), os.getpid(), owner)


def _live_env_marker() -> str:
    """The env marker if it is live; a leaked one is healed away and reads as absent."""
    marker = os.environ.get(DELEGATED_CHILD_ENV_MARKER, "")
    if marker and not env_marker_is_live():
        _heal_leaked_marker()
        return ""
    return marker


def is_delegated_child_process_context() -> bool:
    """Return True in this process or a subprocess spawned by a child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get()) or bool(_live_env_marker())


def _fenced_kanban_root() -> str:
    """The board root this process's Kanban lineage lives under (``kanban_home()``); ``"1"`` when it
    cannot be resolved, which readers treat as "fence every board" (the pre-path marker)."""
    try:
        from hermes_cli.kanban_db import kanban_home
        return str(kanban_home())
    except Exception:
        return "1"


def scrub_kanban_env(env: Mapping[str, str] | MutableMapping[str, str]) -> dict[str, str]:
    """Remove worker identity, retaining board/location and an inherited write fence.

    TASK absence alone would promote a descendant to an orchestrator. The marker
    survives later execs, including scripts that remove TASK themselves. This is
    cooperative runtime scoping, not confinement of code with direct SQLite access.

    The marker's value is the fenced board ROOT, so the fence applies to the lineage's
    board and not to every Kanban DB the descendant touches: a child running a repro
    against a temp ``HERMES_HOME`` got a silently read-only board there. An inherited
    path-valued marker is kept (a grandchild that moved HERMES_HOME must not re-fence
    onto its scratch root and unfence the real one).
    """
    cleaned = {k: v for k, v in env.items() if k not in KANBAN_ENV_KEYS}
    # Only the gateway mints ``kanban`` sessions: a descendant ``hermes chat`` sending it is refused.
    if cleaned.get("HERMES_SESSION_SOURCE") == "kanban":
        del cleaned["HERMES_SESSION_SOURCE"]
    inherited = str(env.get(DELEGATED_CHILD_ENV_MARKER) or "")
    cleaned[DELEGATED_CHILD_ENV_MARKER] = inherited if inherited and inherited != "1" else _fenced_kanban_root()
    if not (cleaned.get(DELEGATED_CHILD_OWNER_ENV) or "").strip():
        cleaned[DELEGATED_CHILD_OWNER_ENV] = _owner_token()
    worker_task = _worker_task_for_children(env)
    if worker_task:
        cleaned[KANBAN_WORKER_TASK_ENV] = worker_task
    return cleaned


def _worker_task_for_children(env: Mapping[str, str]) -> str:
    """[daveey] The card id to export as :data:`KANBAN_WORKER_TASK_ENV` to a scrubbed child.

    The worker's own ``HERMES_KANBAN_TASK`` first: from *env*, else from this process, since some
    callers pass an env already filtered of ``HERMES_*`` (execute_code's sandbox). Then an inherited
    marker, so a worker terminal's own descendants keep naming the card. ``""`` outside any worker
    (a delegate child of an interactive chat has no card)."""
    for key in ("HERMES_KANBAN_TASK", KANBAN_WORKER_TASK_ENV):
        for source in (env, os.environ):
            value = (source.get(key) or "").strip()
            if value:
                return value
    return ""


def kanban_path_is_fenced(path: os.PathLike[str] | str) -> bool:
    """Whether Kanban mutations at *path* (a board DB or board-metadata root) are denied for this
    process: always for an in-process delegate child (the parent's own board); for a spawned
    descendant only when *path* is the dispatcher-pinned ``HERMES_KANBAN_DB`` or lies under the
    fenced root the marker carries. A legacy ``"1"`` marker fences everything.

    One exemption under the root: an entry of the root's auto-pruned scratch dir
    (``<root>/cache/scratch``, ``hermes_constants.get_scratch_dir(root)``, the worker's ``TMPDIR``), so a
    throwaway probe home made with ``mktemp -d "$TMPDIR/kanban-probe.XXXX"`` is writable and the 24 h idle
    pruner removes it. A probe home in ``~`` needed an ``rm -rf`` that unattended runs may not do and leaked
    0.1-2.3 GB per probe on pool workers (t_01a1d3b5bfc898e7). Symlinks resolve first, and the pinned DB,
    the legacy marker and in-process delegate children never get the exemption. [daveey]"""
    if _DELEGATED_CHILD_CONTEXT.get():
        return True
    marker = _live_env_marker()
    if not marker:
        return False
    if marker == "1":
        return True
    from pathlib import Path
    target = Path(path).expanduser().resolve()
    pinned = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if pinned and target == Path(pinned).expanduser().resolve():
        return True
    root = Path(marker).expanduser().resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return False
    from hermes_constants import SPOTLIGHT_NOINDEX_SUFFIX

    # Never resolve arbitrary scratch links into an exemption. Accept only the sibling link
    # shape ensure_spotlight_noindex_dir creates/recognizes on Darwin, including its physical spelling.
    scratch = root / "cache" / "scratch"
    candidates = [scratch]
    sibling = scratch.with_name(scratch.name + SPOTLIGHT_NOINDEX_SUFFIX)
    try:
        if scratch.is_symlink() and os.readlink(scratch) in (sibling.name, str(sibling)):
            candidates.append(sibling)
    except OSError:
        pass  # A changed/unreadable link establishes no additional exemption.
    return not any(target != candidate and target.is_relative_to(candidate) for candidate in candidates)


@overload
def delegated_child_subprocess_env(env: Mapping[str, str]) -> dict[str, str]: ...


@overload
def delegated_child_subprocess_env(env: None = None) -> dict[str, str] | None: ...


def delegated_child_subprocess_env(
    env: Mapping[str, str] | MutableMapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Carry worker/delegate descendant denial across a real process spawn.

    Location and credentials are untouched; callers retain their existing secret policy.
    Dispatcher workers and supervised tool transports grant their own explicit scope.

    Accepted limit [daveey]: this fence is cooperative. Anything the descendant runs as the same
    OS user can drop it (``env -u HERMES_DELEGATED_CHILD_CONTEXT hermes kanban ...``) or skip it
    (``sqlite3 <board>/kanban.db``), because it owns write access to the board files. No layer
    inside Hermes can close that; only OS confinement can (a separate uid or a sandbox without the
    board mounted). The fence exists to stop an agent from writing the board by accident or by
    habit, steering it to the ``kanban_*`` tools, whose ownership checks are the real contract.
    """
    if not (is_delegated_child_process_context() or os.environ.get("HERMES_KANBAN_TASK")
            or (env and (env.get("HERMES_KANBAN_TASK")
                         or (env.get(DELEGATED_CHILD_ENV_MARKER) and env_marker_is_live(env))))):
        if env is None:
            return None
        out = dict(env)
        if out.get(DELEGATED_CHILD_ENV_MARKER):  # leaked into a caller-built env: don't pass it on
            strip_agent_scope_env(out, preserve_board_location=True)
        return out
    return scrub_kanban_env(os.environ if env is None else env)
