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
process whose environment can't be read, the depth cap) records ``agent``, and so does a walk that
meets a non-interactive automation runner (:data:`AUTOMATION_RUNNERS`: SSM SendCommand, cron, atd)
or a comment whose ``--author`` is an agent handle; relays need not forward markers for those.
ROOT's unreadable
process is skipped (``login``/``sshd``, launchd, a setuid ``login`` whose effective uid is root, a
non-dumpable ``sshd-session`` whose environ is root's). Linux also skips a same-uid
``systemd --user`` with permitted capabilities beyond ours when environment access is denied:
CAP_WAKE_ALARM can deny ptrace even though its environ is user-owned. Third-uid managers and
ordinary unreadable processes remain inconclusive; readable markers always win. Agent user
units carry their markers below the manager.
The ``/kanban`` slash command typed into a chat composer passes ``human`` explicitly
(``run_slash(human_typed=True)`` at its ingress points). Fail-safe direction: a leaked marker (a
terminal app started from an agent, the TUI's ``!`` shell) labels a person's note ``agent``, which
only withholds operator authority; so does Windows, where an unreadable or exited ancestor (the
parent of ``explorer.exe``) leaves every walk inconclusive. Out of reach: a same-user process that
deliberately destroys its marked ancestry (``exec env -i`` replacing the tool shell, double-fork
daemonizing) or hands its command to a process outside its ancestry (``tmux send-keys``,
``ssh <host> hermes kanban comment``, whose remote sshd session starts clean) — evasion or a
handoff, not provenance, which needs real authentication. The dashboard API uses this ancestry
reader for resolved raw loopback TCP peers (``kanban_http_comment_writer``); remote/unresolvable
HTTP peers retain the legacy human default, not a claim of authenticated human identity.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
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

# [daveey] Non-interactive automation runners: a command they started is a scheduled job or a
# remote automation (hub-worker-exec's SSM SendCommand), never a person typing, even when the runner
# is root's and its environment unreadable. Relays (ssh, the courier, SSM) each had to forward the
# caller's markers, and each new relay defaulted to ``human``; this rule lives in the writer. Keyed
# by the 15-char ``comm`` prefix too: Linux truncates it when psutil can't widen it from cmdline.
AUTOMATION_RUNNERS: tuple[str, ...] = ("amazon-ssm-agent", "ssm-document-worker", "cron", "crond", "atd")
RUNNER_FOR_NAME: dict[str, str] = {**{r[:15]: r for r in AUTOMATION_RUNNERS},
                                   **{r: r for r in AUTOMATION_RUNNERS}}
# An interactive SSM session (``aws ssm start-session``) is a person's shell; its ancestors are the
# same ssm-agent-worker/amazon-ssm-agent that start SendCommand documents, so no runner above it
# counts and the walk goes on to init exactly as before.
INTERACTIVE_SSM = "ssm-session-worker"

# Agent comment handles: ``<gateway>/t_<16 hex>`` (a worker run's note) and ``hub-chat-<hex>``.
_AGENT_HANDLE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/t_[0-9a-f]{16}|hub-chat-[0-9a-f]{6,})")

logger = logging.getLogger(__name__)


class Inconclusive(Exception):
    """The ancestry could not be read far enough to rule an agent out."""


# (ppid, start-time environ, or None for a skipped OS supervisor) for a pid. Raises
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
        if isinstance(exc, (psutil.AccessDenied, PermissionError)) and _capable_user_manager(proc, pid):
            return ppid, None
        raise Inconclusive(f"pid {pid}: environment unreadable") from exc


def _capable_user_manager(proc, pid: int) -> bool:
    """Recognize Linux's same-uid user manager at a capability/ptrace boundary.

    commoncap's PTRACE_MODE_READ_FSCREDS check compares the reader's effective
    capabilities with the target's *permitted* set. Use our permitted set as a
    stricter bound: only skip a manager with privilege we cannot even enable.
    Do not skip arbitrary capable services (which can themselves be agents).
    This is a provenance heuristic, not authentication against forged argv.
    """
    import psutil

    try:
        uid = getattr(os, "getuid", lambda: 0)()
        if uid == 0 or tuple(proc.uids()) != (uid, uid, uid):
            return False
        status = dict(line.split(":", 1) for line in
                      Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines() if ":" in line)
        own = dict(line.split(":", 1) for line in
                   Path("/proc/self/status").read_text(encoding="utf-8", errors="replace").splitlines() if ":" in line)
        if status.get("Name", "").strip() != "systemd":
            return False
        if not (int(status["CapPrm"], 16) & ~int(own["CapPrm"], 16)):
            return False
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
        return len(argv) == 2 and argv[0].rsplit(b"/", 1)[-1] == b"systemd" and argv[1] == b"--user"
    except (psutil.Error, OSError, ValueError, KeyError, AttributeError):
        return False


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


def _proc_name(pid: int) -> Optional[str]:
    """The process name (psutil widens Linux's 15-char ``comm`` from cmdline), None if unreadable:
    an unknown name only means the runner rule doesn't apply, never a less conclusive walk."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        return str(psutil.Process(pid).name() or "") or None
    except (psutil.Error, OSError, AttributeError):
        return None


_REAL_READ = _read_proc


def agent_ancestor(pid: Optional[int] = None, *, read: Optional[ProcReader] = None) -> Optional[str]:
    """``"<pid>:<MARKER>"`` for the nearest process from ``pid`` (default: this one) up to init
    whose start-time environment carries an agent marker, or ``"<pid>:runner:<name>"`` for a
    non-interactive automation runner (:data:`AUTOMATION_RUNNERS`); None after a complete clean
    walk. Raises :class:`Inconclusive` when the walk can't be completed. Process names are read only
    with the real reader: a substituted reader models its own pids, which real names don't match."""
    read = read or _read_proc
    name_of = _proc_name if read is _REAL_READ else None
    current = os.getpid() if pid is None else int(pid)
    seen: set[int] = set()
    interactive = False
    for _ in range(MAX_ANCESTRY):
        if current in seen:
            raise Inconclusive(f"ancestry loop at pid {current}")
        seen.add(current)
        ppid, environ = read(current)
        marker = env_agent_marker(environ)
        if marker:
            return f"{current}:{marker}"
        name = name_of(current) if name_of else None
        if name == INTERACTIVE_SSM:
            interactive = True
        runner = RUNNER_FOR_NAME.get(name or "")
        if runner and not interactive:
            return f"{current}:runner:{runner}"
        if current == 1 or ppid <= 0:
            return None  # reached init (or a root with no parent): complete
        current = ppid
    raise Inconclusive(f"ancestry deeper than {MAX_ANCESTRY}")


def is_agent_handle(author: Optional[str]) -> bool:
    """True for an agent comment handle: ``<gateway>/t_<16 hex>`` or ``hub-chat-<6+ hex>``."""
    return _AGENT_HANDLE.fullmatch(author or "") is not None


# [daveey] A systemd service (a timer's job, the gateway and its plugins) is started by the service
# manager, never typed by a person inside it; a login/ssh shell lives in ``session-N.scope`` and a
# terminal app in its own ``.scope``, which keep the walk. Linux cgroup v2 only.
_SYSTEMD_SERVICE = re.compile(r"/(?:system\.slice|user\.slice/(?:[^/]+/)*user@\d+\.service)/(?:[^/]+/)*"
                              r"(?P<unit>[^/]+\.service)")


def cgroup_v2_path(text: str) -> Optional[str]:
    """The unified (``0::``) hierarchy path from ``/proc/<pid>/cgroup`` text, else None."""
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or None
    return None


def _self_cgroup() -> Optional[str]:
    try:
        return cgroup_v2_path(Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def systemd_service_unit(path: Optional[str]) -> Optional[str]:
    """The ``.service`` unit whose cgroup leaf ``path`` is (under system.slice or a user manager)."""
    match = _SYSTEMD_SERVICE.fullmatch(path or "")
    return match.group("unit") if match else None


def cli_comment_writer(*, read: Optional[ProcReader] = None, env: Optional[Mapping[str, str]] = None,
                       author: Optional[str] = None,
                       cgroup: Optional[Callable[[], Optional[str]]] = None) -> tuple[str, Optional[str], str]:
    """``(writer_kind, writer_session, evidence)`` for a comment written by this CLI process.

    ``agent`` when this process or any ancestor started with an agent marker or under an
    automation runner, when this process runs in a systemd ``.service`` (``cgroup``: its cgroup v2
    path; the real one only with the real reader), or when that can't be ruled out, or when
    ``author`` is an agent handle (:func:`is_agent_handle`); each only ever lowers authority.
    ``human`` only after a complete walk found none. ``writer_session`` is the agent's inherited
    ``HERMES_SESSION_ID`` when it has one."""
    env = os.environ if env is None else env
    if cgroup is None and (read or _read_proc) is _REAL_READ:
        cgroup = _self_cgroup
    try:
        found = agent_ancestor(read=read)
    except Inconclusive as exc:
        found = f"inconclusive:{exc}"
    unit = systemd_service_unit(cgroup()) if found is None and cgroup else None
    if unit:
        found = f"runner:systemd:{unit}"
    if found is None and is_agent_handle(author):
        logger.info("comment_writer: author handle overrides human walk")
        found = "author_handle"
    if found is None:
        return "human", None, "clean-ancestry"
    session = str(env.get("HERMES_SESSION_ID") or "").strip()[:200] or None
    return "agent", session, found
