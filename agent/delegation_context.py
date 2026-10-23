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


def strip_agent_scope_env(env: MutableMapping[str, str]) -> list[str]:
    """Remove the agent-scope family from *env* in place when a worker marker is present.

    Returns the removed key names (empty when nothing leaked). The single list every top-level
    launch path (CLI entry, Desktop relaunch, Electron main, hermes-sync) uses.
    """
    if not any(env.get(key) for key in AGENT_SCOPE_MARKER_KEYS):
        return []
    removed = [key for key in list(env) if is_agent_scope_env_key(key)]
    for key in removed:
        env.pop(key, None)
    return removed


@contextmanager
def delegated_child_context(session_id: str | None = None) -> Iterator[None]:
    """Mark child execution and isolate its task-local session identity. Even a context
    entered without an id must restore the parent's session ContextVar (child
    construction calls ``set_current_session_id``)."""
    token = _DELEGATED_CHILD_CONTEXT.set(True)
    try:
        from gateway.session_context import scoped_current_session_id  # lazy: it calls is_delegated_child_context()

        with scoped_current_session_id(session_id):
            yield
    finally:
        _DELEGATED_CHILD_CONTEXT.reset(token)


def is_delegated_child_context() -> bool:
    """Return True while code is running for a delegate_task child."""
    return bool(_DELEGATED_CHILD_CONTEXT.get())


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
    current = os.getpid()
    for _ in range(64):
        if current == owner:
            return True
        parent = _parent_pid(current)
        if not parent or parent == current:
            return False
        current = parent
    return False


def _owner_is_live_ancestor(owner: str) -> bool:
    pid_s, _, fp_s = owner.partition(":")
    try:
        pid = int(pid_s)
    except ValueError:
        return True  # unreadable owner: fail closed (keep the fence)
    if fp_s:
        current = _start_fingerprint(pid)
        if current is None:
            return False  # owner pid is gone
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
    removed = strip_agent_scope_env(os.environ)
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
    inherited = str(env.get(DELEGATED_CHILD_ENV_MARKER) or "")
    cleaned[DELEGATED_CHILD_ENV_MARKER] = inherited if inherited and inherited != "1" else _fenced_kanban_root()
    if not (cleaned.get(DELEGATED_CHILD_OWNER_ENV) or "").strip():
        cleaned[DELEGATED_CHILD_OWNER_ENV] = _owner_token()
    return cleaned


def kanban_path_is_fenced(path: os.PathLike[str] | str) -> bool:
    """Whether Kanban mutations at *path* (a board DB or board-metadata root) are denied for this
    process: always for an in-process delegate child (the parent's own board); for a spawned
    descendant only when *path* is the dispatcher-pinned ``HERMES_KANBAN_DB`` or lies under the
    fenced root the marker carries. A legacy ``"1"`` marker fences everything."""
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
    try:
        target.relative_to(Path(marker).expanduser().resolve())
    except ValueError:
        return False
    return True


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
    """
    if not (is_delegated_child_process_context() or os.environ.get("HERMES_KANBAN_TASK")
            or (env and (env.get("HERMES_KANBAN_TASK")
                         or (env.get(DELEGATED_CHILD_ENV_MARKER) and env_marker_is_live(env))))):
        if env is None:
            return None
        out = dict(env)
        if out.get(DELEGATED_CHILD_ENV_MARKER):  # leaked into a caller-built env: don't pass it on
            strip_agent_scope_env(out)
        return out
    return scrub_kanban_env(os.environ if env is None else env)
