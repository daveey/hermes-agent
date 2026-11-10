"""Who ran ``hermes kanban comment``: an agent's tool subprocess or a person's shell. [daveey]

The ``commented`` event's ``writer_kind`` decides whether a worker hears a note "from the operator"
(and the ask-gate treats it as David's words). The CLI hardcoded ``human``, yet agents run it too:
a terminal-tool command, an ``execute_code`` script, a plugin subprocess of the gateway, a
delegated child, another harness (Claude Code, Codex) driving ``hermes``. Their notes reached
workers as the operator's.

The signal is the agent-harness environment Hermes gives every tool subprocess, read from the
process ANCESTRY's start-time environments, not from this process's mutable ``os.environ``:

* every hermes entrypoint ``setdefault``s ``HERMES_AGENT``/``AI_AGENT`` in its own ``os.environ``
  (so the CLI's own value is useless); the start-time environment (``/proc/<pid>/environ``,
  ``KERN_PROCARGS2`` on macOS) is what the parent passed to ``exec`` and never changes later;
* the terminal tool's shell (local backend: ``build_subprocess_env`` copies the agent's
  ``os.environ``), the ``execute_code`` child, a worker's tools, a delegated child and the supervised
  gateway service are each started with one of :data:`AGENT_ENV_MARKERS`; a person's login shell,
  ssh session or the Desktop terminal pane (launched by launchd/sshd/Electron) carries none;
* walking the ancestors means ``env -i hermes kanban comment`` or ``unset HERMES_AGENT`` inside the
  agent's shell still finds the agent: the shell that ran it was itself started with the markers.

No input asserts ``human``: no flag, no variable. Human is only a COMPLETE walk to init that found
no marker; an inconclusive walk (psutil unusable, an ancestor vanishing mid-walk, a same-user
process whose environment can't be read, the depth cap) records ``agent``. Only ROOT's unreadable
process is skipped (``login``/``sshd``, launchd, a setuid ``login`` whose effective uid is root, a
non-dumpable ``sshd-session`` whose environ is root's): an agent's own processes are this user's
and readable. The ``/kanban`` slash command typed into a chat composer passes ``human`` explicitly
(``run_slash(human_typed=True)`` at its ingress points). Fail-safe direction: a leaked marker (a
terminal app started from an agent, the TUI's ``!`` shell) labels a person's note ``agent``, which
only withholds operator authority; so does Windows, where an unreadable or exited ancestor (the
parent of ``explorer.exe``) leaves every walk inconclusive. Out of reach: a same-user process that
deliberately destroys its marked ancestry (``exec env -i`` replacing the tool shell, double-fork
daemonizing) or hands its command to a process outside its ancestry (``tmux send-keys``, ``at``,
``ssh <host> hermes kanban comment``, whose remote sshd session starts clean) — evasion or a
handoff, not provenance, which needs real authentication. Not covered here: the dashboard API
(``POST /api/plugins/kanban/tasks/<id>/comments``) still records ``human`` for any token holder
that omits ``writer_kind`` (``plugin_api_comment_actor``); that server-side gap is a follow-up.
"""
from __future__ import annotations

import os
from typing import Callable, Mapping, Optional

# Any one, non-empty, in a process's start-time environment marks it (and its descendants) as an
# agent's. HERMES_AGENT/AI_AGENT: every terminal-tool shell exports them (BaseEnvironment
# ._wrap_command) and every hermes process inherits them into its children; AI_AGENT also names
# other harnesses. The rest are Hermes' per-context tool-subprocess identities.
AGENT_ENV_MARKERS: tuple[str, ...] = (
    "HERMES_KANBAN_WORKER_TASK",       # a Kanban worker's tool subprocesses
    "HERMES_KANBAN_TASK",              # the worker process itself
    "HERMES_DELEGATED_CHILD_CONTEXT",  # a delegate_task child's subprocesses
    "HERMES_RPC_SOCKET",               # an execute_code script
    "HERMES_SESSION_ID",               # a chat agent's terminal tool (when its session is bound)
    "HERMES_AGENT",                    # Hermes harness attribution (always in tool shells)
    "AI_AGENT",                        # cross-harness attribution standard
    "CLAUDECODE",                      # Claude Code's Bash tool
    "HERMES_SUPERVISED_CHILD",         # the systemd/launchd gateway service and all it spawns
    "HERMES_DESKTOP",                  # a Desktop agent backend (`serve`), never the GUI's terminal
)

MAX_ANCESTRY = 64


class Inconclusive(Exception):
    """The ancestry could not be read far enough to rule an agent out."""


# (ppid, start-time environ, or None for root's unreadable process) for a pid. Raises
# :class:`Inconclusive` when the pid is gone or any other process can't be read.
ProcReader = Callable[[int], tuple[int, Optional[Mapping[str, str]]]]


def env_agent_marker(env: Optional[Mapping[str, str]]) -> Optional[str]:
    """The first :data:`AGENT_ENV_MARKERS` name set (non-blank) in ``env``, else None."""
    if not env:
        return None
    for name in AGENT_ENV_MARKERS:
        if str(env.get(name) or "").strip():
            return name
    return None


def _read_proc(pid: int) -> tuple[int, Optional[Mapping[str, str]]]:
    try:
        import psutil
    except ImportError as exc:
        raise Inconclusive("psutil unavailable") from exc
    try:
        proc = psutil.Process(pid)
        ppid = proc.ppid()
    except psutil.Error as exc:
        raise Inconclusive(f"pid {pid}: {type(exc).__name__}") from exc
    try:
        return ppid, proc.environ()
    except (psutil.Error, OSError) as exc:
        if _foreign(proc, pid):  # root's login/sshd, launchd, a non-dumpable sshd-session
            return ppid, None
        raise Inconclusive(f"pid {pid}: environment unreadable") from exc


def _foreign(proc, pid: int) -> bool:
    """True when ``proc`` is root's rather than this user's: any of its real/effective/saved uids
    is root (``sshd``, launchd, macOS setuid ``login``, which keeps the user's real uid), or
    (Linux) its ``environ`` is owned by root (a non-dumpable process such as ``sshd-session``,
    whose memory is root's). Only ROOT counts: a process of a third uid stays inconclusive, so
    ``sudo -u <other> hermes kanban comment`` (every agent process looks foreign to <other>)
    can't walk past the agent to a clean init. An agent's own processes are this user's and
    dumpable. POSIX only: on Windows an unreadable process stays inconclusive."""
    getuid = getattr(os, "getuid", None)
    if getuid is None or getuid() == 0:
        return False  # root reads every environ; nothing above it is foreign
    try:
        uids = tuple(proc.uids())
    except Exception:  # health: allow BLE001 -- psutil/OS variance: fall through to the stat check
        uids = ()
    if 0 in uids:
        return True
    try:
        return os.stat(f"/proc/{pid}/environ").st_uid == 0
    except OSError:
        return False


def agent_ancestor(pid: Optional[int] = None, *, read: Optional[ProcReader] = None) -> Optional[str]:
    """``"<pid>:<MARKER>"`` for the nearest process from ``pid`` (default: this one) up to init
    whose start-time environment carries an agent marker; None after a complete clean walk.
    Raises :class:`Inconclusive` when the walk can't be completed."""
    read = read or _read_proc
    current = os.getpid() if pid is None else int(pid)
    seen: set[int] = set()
    for _ in range(MAX_ANCESTRY):
        if current in seen:
            raise Inconclusive(f"ancestry loop at pid {current}")
        seen.add(current)
        ppid, environ = read(current)
        marker = env_agent_marker(environ)
        if marker:
            return f"{current}:{marker}"
        if current == 1 or ppid <= 0:
            return None  # reached init (or a root with no parent): complete
        current = ppid
    raise Inconclusive(f"ancestry deeper than {MAX_ANCESTRY}")


def cli_comment_writer(*, read: Optional[ProcReader] = None,
                       env: Optional[Mapping[str, str]] = None) -> tuple[str, Optional[str], str]:
    """``(writer_kind, writer_session, evidence)`` for a comment written by this CLI process.

    ``agent`` when this process or any ancestor started with an agent marker, or when that can't
    be ruled out; ``human`` only after a complete walk found none. ``writer_session`` is the agent's
    inherited ``HERMES_SESSION_ID`` when it has one."""
    env = os.environ if env is None else env
    try:
        found = agent_ancestor(read=read)
    except Inconclusive as exc:
        found = f"inconclusive:{exc}"
    if found is None:
        return "human", None, "clean-ancestry"
    session = str(env.get("HERMES_SESSION_ID") or "").strip()[:200] or None
    return "agent", session, found
