"""[daveey] Terminal processes that survive a Hermes update (t_01a1c7d04d9046d2).

A gateway/backend restart (every hub core update) stopped the unit the terminal tool's children ran in,
so a background job or a long foreground command died with it. ``terminal.update_survivable_processes:
true`` (default off; Linux with a usable ``systemd-run --user --scope`` only) changes that:

- every ``background=true`` job defaults to ``persist_on_release`` (an explicit ``false`` still wins), so
  it runs in its own transient scope with a file-backed log and exit file, and the next backend re-attaches
  it from ``processes.json`` (``recover_from_checkpoint``) and delivers its completion;
- a foreground command whose effective timeout is above ``FOREGROUND_MIN_TIMEOUT_S`` runs in its own scope
  too, its output teed to such a log, and is held in the registry's checkpoint while the tool waits on it
  (``ProcessForegroundHoldMixin``). When a Hermes update pause, a gateway shutdown or a backend exit
  interrupts the call (``SURVIVABLE_INTERRUPT_REASONS``) the process is not killed: it becomes a
  registered background job of the session, notified on exit, and the tool result says
  ``process continues as <session_id>``. A user Stop (or any other interrupt, or the timeout) kills it
  as before.

The key on with no usable scope keeps today's behaviour and logs one WARNING.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tools.process_registry")

CONFIG_KEY = "update_survivable_processes"
FOREGROUND_MIN_TIMEOUT_S = 60
CONTINUES_LINE = "process continues as {session_id}"

_warn_lock = threading.Lock()
_warned = False


def enabled() -> bool:
    """``terminal.update_survivable_processes`` is on and this is Linux (no scope probe)."""
    from tools import process_registry as pr

    if not pr._IS_LINUX:
        return False
    try:
        value = pr.ProcessRegistry._config_value("terminal", CONFIG_KEY, False)
    except Exception:  # an unreadable config.yaml keeps today's behaviour
        logger.debug("terminal.%s unreadable; treated as off", CONFIG_KEY, exc_info=True)
        return False
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on"))


def usable() -> bool:
    """The key is on and a transient user scope can be created. The key on without one logs a single
    WARNING per process and keeps today's behaviour."""
    global _warned
    if not enabled():
        return False
    from tools import process_registry as pr

    from tools.process_registry_persist_scope import kanban_run_scope_cgroup, user_unit_cgroup

    # The same decision persisted_job_scope_argv makes: a process in no user unit gets no scope.
    in_unit = kanban_run_scope_cgroup() is not None or user_unit_cgroup() is not None
    # The probe resolves systemd-run itself (no binary = unusable).
    if in_unit and pr._systemd_run_user_scope_available():
        return True
    with _warn_lock:
        if not _warned:
            _warned = True
            logger.warning(
                "terminal.update_survivable_processes is on but systemd-run --user --scope is unusable here "
                "(no systemd-run, no reachable user D-Bus session, or this process runs in no systemd user "
                "unit): terminal processes keep dying with the backend on a restart")
    return False


def default_persist(requested: Optional[bool], env_type: str, pty: bool = False) -> bool:
    """``persist_on_release`` for a ``background=true`` spawn: the caller's explicit value, else on for
    a local pipe job while survivable processes are usable. A ``pty=true`` job keeps its PTY (a persisted
    job runs on a log file instead, which would break the interactive CLI it drives) and today's fate."""
    if requested is not None:
        return bool(requested)
    return env_type == "local" and not pty and usable()


def foreground_eligible(env: Any, env_type: str, effective_timeout: Any) -> bool:
    """A foreground call that runs scoped and survivable (local backend, timeout above the floor)."""
    try:
        long_enough = float(effective_timeout) > FOREGROUND_MIN_TIMEOUT_S
    except (TypeError, ValueError):
        return False
    if env_type != "local" or not long_enough:
        return False
    from tools.environments.local import LocalEnvironment

    return isinstance(env, LocalEnvironment) and usable()


def survivable_interrupt_reason(*thread_ids: Optional[int]) -> Optional[str]:
    """The pending interrupt of ``thread_ids`` (the waiter, the tool thread it acts for) when it is one a
    survivable command outlives; None for any other interrupt (a user Stop kills the command)."""
    # ``SURVIVABLE_INTERRUPT_REASONS``: the ``tool_reason`` slugs a survivable foreground command outlives
    # (card A's update pause, the gateway's restart/shutdown stop, a TUI/Desktop backend exit). A user Stop,
    # a new message, a timeout and every watchdog are not in it: those still kill the command. Imported here,
    # not at module level: terminal_tool imports this module, and harnesses stub tools.interrupt.
    from tools.interrupt import SURVIVABLE_INTERRUPT_REASONS, thread_interrupt_reason

    for tid in thread_ids:
        if tid and (reason := thread_interrupt_reason(tid)) in SURVIVABLE_INTERRUPT_REASONS:
            return reason
    return None


TEE_DRAIN_WAIT_TICKS = 10  # x 0.1 s: tee normally drains in milliseconds; a daemon holding stdout keeps it open


def tee_wrapped(cmd_string: str, log: Path) -> str:
    """``cmd_string`` with its stdout+stderr teed into ``log`` and its exit status written beside it.
    ``tee -p`` ignores a closed pipe: once the waiting backend is gone the command keeps writing its log
    instead of dying of SIGPIPE, and the next backend tails that log. bash does not wait for the tee (a
    process substitution) beyond a short drain: a command that leaves a daemon holding its stdout
    (``server &``) returns when bash exits, as without the tee, instead of waiting out the timeout."""
    from tools.process_registry_output_log import _exit_path

    exit_file = shlex.quote(_exit_path(log).as_posix())
    return "\n".join((
        f"exec 3> >(exec tee -p -a {shlex.quote(str(log))}); __hermes_tee=$!",
        f"( {cmd_string}\n) >&3 2>&3 3>&-",  # a subshell: the wrapper script ends in its own ``exit``
        "__hermes_rc=$?; exec 3>&-",
        f"for _ in $(seq {TEE_DRAIN_WAIT_TICKS}); do kill -0 \"$__hermes_tee\" 2>/dev/null || break; sleep 0.1; done",
        f"printf '%s\\n' \"$__hermes_rc\" > {exit_file}; exit \"$__hermes_rc\""))


class SurvivableForeground:
    """One survivable foreground spawn: ``LocalEnvironment._run_bash`` calls ``spawn``, the wait calls
    ``handoff`` on a survivable interrupt, and the terminal tool calls ``release`` once the wait returned."""

    def __init__(self, *, command: str, task_id: str, owner_task_id: str, session_key: str):
        self.command, self.task_id, self.owner_task_id, self.session_key = command, task_id, owner_task_id, session_key
        self.session = None
        self.converted = False

    def spawn(self, args: list[str], run_env: dict, cwd: str, stdin_data: Optional[str],
              markers: tuple = ()) -> Optional[subprocess.Popen]:
        """Start ``args`` (``[bash, ..., "-c", script]``) in its own scope, held in the registry; None when
        no scope can be had any more (the caller spawns as usual)."""
        from tools.process_registry import process_registry, systemd_user_bus_env
        from tools.process_registry_output_log import worker_output_log
        from tools.process_registry_persist_scope import persisted_job_scope_argv

        session = process_registry._new_session(self.command, self.task_id, self.owner_task_id, self.session_key,
                                                cwd, persist_on_release=True, shell_markers=[m for m in markers if m])
        log = worker_output_log(session.id, persist=True)
        if log is None:
            return None
        shell = [*args[:-1], tee_wrapped(args[-1], log)]
        argv = persisted_job_scope_argv(session, shell, session.id)
        if argv is shell:
            return None
        from gateway.session_context import async_delivery_supported, get_session_env

        # noqa: subprocess-stdin — stdin is DEVNULL or the caller's piped data
        proc = subprocess.Popen(
            argv, text=True, env=systemd_user_bus_env(run_env), encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            # detached-spawn: ok — survives its owner on purpose; a kill/timeout/Stop reaps its group and scope
            start_new_session=True, cwd=cwd)
        with suppress(ProcessLookupError):
            proc._hermes_pgid = os.getpgid(proc.pid)
            from gateway.status import get_process_start_time
            proc._hermes_pgid_start = get_process_start_time(proc.pid)
        proc._hermes_survivable = self
        session.process, session.pid, session.output_log = proc, proc.pid, str(log)
        session.host_start_time = process_registry._safe_host_start_time(proc.pid)
        # A crash of this backend mid-call leaves the job to the next one, which notifies the session.
        session.notify_on_complete = async_delivery_supported()
        if session.notify_on_complete:
            from tools.terminal_tool_background import _stamp_gateway_routing
            _stamp_gateway_routing(session, get_session_env)
        self.session = session
        process_registry.hold_foreground(session)
        return proc

    def handoff(self, proc: subprocess.Popen, output_so_far: str, *, yielded: bool = False,
                reason: Optional[str] = None) -> Optional[dict]:
        """Keep the process as a background job of the session: the wait was interrupted by an
        update/shutdown, or (``yielded``) a user message asked the command to yield to background, which
        for a survivable command reuses its scope and log instead of adopting the pipe."""
        from tools.process_registry import process_registry

        session = self.session
        if session is None:
            return None
        # Our pipe end closes: tee keeps writing the log, which the registry's reader tails from here on.
        with suppress(Exception):
            proc.stdout.close()
        process_registry.convert_foreground(session)
        self.converted = True
        if session.notify_on_complete and session.watcher_platform:
            from tools.terminal_tool_background import _register_completion_watcher
            _register_completion_watcher(process_registry, session, self.session_key)
        logger.info("survivable foreground command kept running as %s (pid %s): %s", session.id, session.pid,
                    "yielded to background" if yielded else "update/shutdown interrupt")
        key = "yielded_session_id" if yielded else "survived_session_id"
        return {"output": output_so_far, "returncode": None, key: session.id, "pid": session.pid,
                "notify_on_complete": session.notify_on_complete, "interrupt_reason": reason}

    def release(self, result: Optional[dict]) -> None:
        """The wait returned without a handoff: drop the hold and the files; a killed command's scope is
        stopped so a survivor in it is reaped too."""
        from tools.process_registry import _stop_systemd_unit, process_registry
        from tools.process_registry_output_log import _exit_path

        session = self.session
        if session is None or self.converted:
            return
        process_registry.release_foreground(session.id)
        killed = not result or result.get("hermes_interrupted") or result.get("hermes_timed_out")
        if killed and session.systemd_unit:
            _stop_systemd_unit(session.systemd_unit)
        for path in (Path(session.output_log), _exit_path(session.output_log)):
            with suppress(OSError):
                path.unlink()


def survived_result_json(result: dict) -> str:
    """The tool result of a foreground command handed to the background by an update/shutdown: its output,
    then ``process continues as <session_id>`` as the line after the interrupt stamp."""
    sid, reason = result["survived_session_id"], result.get("interrupt_reason") or "a Hermes restart"
    output = (result.get("output") or "").rstrip("\n")
    stamp = f"[interrupted by {reason}]\n{CONTINUES_LINE.format(session_id=sid)}"
    return json.dumps({
        "output": f"{output}\n{stamp}" if output else stamp, "exit_code": None, "error": None,
        "status": "continues_in_background", "session_id": sid, "pid": result.get("pid"),
        "notify_on_complete": bool(result.get("notify_on_complete")),
    }, ensure_ascii=False)


class ProcessForegroundHoldMixin:
    """Survivable foreground commands while the terminal tool waits on them: checkpointed (so a crash of
    this process leaves them to the next owner) but not listed, polled or swept as background jobs."""

    _lock: Any
    _running: dict

    def _holds(self) -> dict:
        holds = vars(self).get("_foreground_holds")
        if holds is None:
            holds = self._foreground_holds = {}
        return holds

    def held_foreground(self) -> list:
        with self._lock:
            return list(self._holds().values())

    def hold_foreground(self, session) -> None:
        with self._lock:
            self._holds()[session.id] = session
        self._write_checkpoint()

    def release_foreground(self, session_id: str) -> None:
        with self._lock:
            dropped = self._holds().pop(session_id, None)
        if dropped is not None:
            self._write_checkpoint(settled_id=session_id)

    def convert_foreground(self, session) -> None:
        """Track a held command as a running background job, tailing its log (the pipe is closed)."""
        with self._lock:
            self._holds().pop(session.id, None)
        self._track_started(session, self._log_reader_loop, f"proc-log-reader-{session.id}")
