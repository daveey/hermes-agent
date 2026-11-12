"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory (`terminal.cwd`
is bridged to it once at gateway/cron startup; the local CLI leaves it unset and relies on
the launch dir). Reading it in one place keeps the system prompt, tool surfaces, and
context-file discovery agreeing on where the agent lives. Multi-session gateways can pin a
logical cwd via `_SESSION_CWD`.
"""

import logging
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from hermes_cli._startup_fast import project_root_str

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)

# The package/source root (<root>/agent/runtime_cwd.py). A backend launched from or
# self-spawned into this tree (desktop default) must never let an os.getcwd() fallback
# inject this repo's contributor AGENTS.md as project context.
_PACKAGE_ROOT = Path(project_root_str())


def _is_install_tree(p: Path) -> bool:
    """True only when ``p`` IS the package root or sits inside it — ancestors
    (a home dir containing the checkout) are legitimate workspaces."""
    try:
        p = p.resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def scoped_session_cwd() -> str:
    """Return the current session's declared cwd without local path validation.

    Remote and container paths may not exist on the Hermes host. Callers that only need
    logical workspace identity should preserve the declared value instead of resolving it.
    """
    value = _SESSION_CWD.get()
    return "" if value is _UNSET else str(value).strip()


def reset_session_cwd(token: Token) -> None:
    """Restore the logical cwd that was active before the matching ``set_session_cwd``."""
    _SESSION_CWD.reset(token)


def scope_terminal_cwd() -> str:
    """Scope-aware TERMINAL_CWD value (may be empty) — every cwd consumer reads through this.

    Under gateway multiplexing the per-turn terminal scope carries the active profile's cwd;
    the process-global env var may hold another profile's. Only an ImportError falls back: an
    active refusal scope must raise, not silently resolve the launch profile's cwd.
    """
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def _existing_dir(raw: str, label: str) -> Path | None:
    p = Path(raw).expanduser()
    if p.is_dir():
        return p
    logger.warning("%s does not exist: %s", label, raw)
    return None


def _resolve_configured_cwd(
    *, override_is_final: bool, include_session_override: bool = True,
) -> Path | None:
    """Session override, then TERMINAL_CWD; each validated as a real directory.

    ``override_is_final``: a set-but-missing session override yields None
    instead of falling through to TERMINAL_CWD.
    ``include_session_override``: skip a session cwd known to be a launch artifact
    while still consulting the active profile's TERMINAL_CWD.
    """
    if include_session_override:
        override = _SESSION_CWD.get()
        override = "" if override is _UNSET else str(override).strip()
        if override:
            p = _existing_dir(override, "configured working directory")
            if p is not None or override_is_final:
                return p
    raw = scope_terminal_cwd().strip()
    return _existing_dir(raw, "TERMINAL_CWD") if raw else None


def process_cwd() -> str:
    """``os.getcwd()``, or ``""`` when the process cwd is gone (deleted worktree/temp dir) or
    unreadable (macOS TCC). The one crash-proof process-cwd read every tool falls back through;
    other OSErrors still propagate so callers see the real problem."""
    try:
        return os.getcwd()
    except (FileNotFoundError, PermissionError):
        return ""


_deleted_cwd_warned = False


def process_cwd_or_fallback() -> str:
    """The process cwd, or — when it is gone (a Kanban workspace removed and recreated mid-run leaves
    the worker in a deleted inode) — the first existing of: the scope's TERMINAL_CWD, the Kanban
    workspace (HERMES_KANBAN_WORKSPACE), $HOME. Logs one warning per process. The turn path (system
    prompt rebuild at compaction, context-file discovery) reads through this so a deleted cwd
    degrades the prompt instead of killing the turn."""
    global _deleted_cwd_warned
    cwd = process_cwd()
    if cwd:
        return cwd
    label, chosen = "HOME", os.path.expanduser("~")
    for name, raw in (("TERMINAL_CWD", scope_terminal_cwd()),
                      ("HERMES_KANBAN_WORKSPACE", os.environ.get("HERMES_KANBAN_WORKSPACE", ""))):
        path = os.path.expanduser(raw.strip()) if raw and raw.strip() else ""
        if path and os.path.isdir(path):
            label, chosen = name, path
            break
    if not _deleted_cwd_warned:
        _deleted_cwd_warned = True
        logger.warning("process working directory was deleted (or is unreadable); using %s=%s instead", label, chosen)
    return chosen


def resolve_agent_cwd() -> Path:
    """Configured cwd, else the launch dir; a deleted launch dir falls back via ``process_cwd_or_fallback``.

    Callers that must act IN the project (goal gates, /init writing AGENTS.md) use
    :func:`resolve_agent_cwd_strict` instead: a fallback directory is a different project there."""
    return _resolve_configured_cwd(override_is_final=False) or Path(process_cwd_or_fallback())


def resolve_agent_cwd_strict() -> Path | None:
    """Configured cwd, else the launch dir, else ``None`` when the launch dir is gone. Never substitutes
    the Kanban workspace or $HOME for a deleted launch dir (TERMINAL_CWD is already the configured cwd
    above), so callers that run or write project files can refuse instead of using another tree."""
    configured = _resolve_configured_cwd(override_is_final=False)
    if configured is not None:
        return configured
    cwd = process_cwd()
    return Path(cwd) if cwd else None


def resolve_context_cwd(*, include_session_override: bool = True) -> Path | None:
    """Configured cwd for context-file discovery, or None (build_context_files_prompt then falls back to the
    launch dir). An existing configured path is honored verbatim — including the Hermes source tree, a
    legitimate workspace when developing Hermes; fallback-directory policy lives in the caller.

    Launch-artifact callers can skip the session override while still honoring the active profile's
    TERMINAL_CWD.
    """
    return _resolve_configured_cwd(
        override_is_final=True, include_session_override=include_session_override,
    )
