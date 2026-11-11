"""File-backed output for background processes a registered worker starts.

A managed worker is a per-turn interpreter. A stdout pipe whose read end that worker's reader
thread owns closes when the worker exits, so the child's next write raises SIGPIPE
(``broken_pipe``) and everything it printed is gone. Under a worker the child writes to a
profile-scoped log instead and its shell records the exit status beside it (the contract the
env-backend wrapper already uses), so whichever interpreter adopts the checkpoint entry next
(the session's next worker, the owner at startup) tails the same file and collects the exit.
"""
import codecs
from contextlib import suppress
import logging
import os
import re
from pathlib import Path
import time
from typing import Optional
from hermes_dir_acl import make_user_dir

from hermes_constants import get_hermes_home

logger = logging.getLogger("tools.process_registry")

_TAIL_SECONDS = 0.2
# After the process ended: an orphaned grandchild may keep appending; stop draining after this.
_FINAL_DRAIN_MAX_BYTES = 1 << 20


def output_dir() -> Path:
    return get_hermes_home() / "logs" / "process-output"


def worker_output_log(session_id: str, *, persist: bool = False) -> Optional[Path]:
    """The log a worker-spawned or ``persist_on_release`` process writes to; None otherwise.

    A persisted job outlives its spawner by contract (a Desktop backend restarted by every hub update,
    2026-11-04): on a pipe its first write after the spawner exits is SIGPIPE, so it logs here too and the
    next backend re-adopts the log (POSIX only: the Windows trap-wrapped log path is untested). [daveey]"""
    from agent.runtime_session_store import is_worker_process
    if not ((persist and os.name != "nt") or is_worker_process()):
        return None
    directory = output_dir()
    make_user_dir(directory, mode=0o700, parents=True, exist_ok=True)
    from tools.process_registry_results import RESULT_RETENTION_SECONDS
    # Only a recorded exit retires a log: a quiet process can run longer than the retention.
    cutoff = time.time() - RESULT_RETENTION_SECONDS
    for exited in directory.glob("proc_*.exit"):
        with suppress(FileNotFoundError):
            if exited.stat().st_mtime < cutoff:
                exited.with_suffix(".log").unlink(missing_ok=True)
                exited.unlink()
    return directory / f"{session_id}.log"


PERSIST_NO_PTY = "persist_on_release jobs write to a log file so they outlive the backend"


def spawn_output_log(session, use_pty: bool) -> Optional[Path]:
    """``worker_output_log`` for a new local spawn. A persisted ``pty=true`` request runs on the log (the
    backend holds a PTY master, so the job would get SIGHUP when it exits) and records why."""
    log = worker_output_log(session.id, persist=session.persist_on_release)
    if log is not None and use_pty and session.persist_on_release:
        session.pty_fallback = PERSIST_NO_PTY
    return log


def adoptable_log(path) -> str:
    """A checkpointed log path, only when it names this profile's output directory."""
    log = Path(str(path or ""))
    return str(log) if path and log.parent == output_dir() and log.suffix == ".log" else ""


def open_output_log(log: Path) -> int:
    """Owner-only append descriptor for the child's stdout+stderr (the parent closes its copy)."""
    return os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)


def _exit_path(log) -> Path:
    return Path(log).with_suffix(".exit")


def record_exit_command(log: Path, command: str) -> str:
    """Prefix ``command`` so the shell writes its exit status beside the log: an interpreter
    that adopts the process after the spawner exited has no Popen to wait on."""
    import shlex
    target = shlex.quote(_exit_path(log).as_posix())
    return f"__hermes_exit_file={target}; trap 'printf \"%s\\n\" \"$?\" > \"$__hermes_exit_file\"' EXIT\n{command}"


def strip_shell_markers(text: str, markers) -> str:
    """Drop the foreground wrapper's own trailer lines (``printf '\\n<m>...<m>\\n'``, base_session_env) for
    exactly ``markers``: a survivable foreground command handed to the background tees them into its log. [daveey]"""
    for marker in markers or ():
        if marker and marker in text:
            text = re.sub(rf"\n{re.escape(marker)}[^\n]*?{re.escape(marker)}\n", "", text)
    return text


def recorded_exit_code(log) -> Optional[int]:
    try:
        return int(_exit_path(log).read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError):
        return None


class ProcessOutputLogMixin:
    """The subclass supplies ``_ingest_output``, ``_clean_shell_noise``, ``_finish_reader``,
    ``_finish_exited`` and ``_detached_host_fate``. See ProcessRegistry."""

    def _log_reader_loop(self, session) -> None:
        """Tail ``session.output_log`` until its process ends. The spawner decides the exit from
        its Popen; an adopter (no Popen) from the recorded status, else ``lost`` (-1)."""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        proc = session.process
        # poll()/wait() reconcile asks this reader to finish instead of reading a pipe itself.
        session._reader_selectable = True

        head_noise = True

        def ingest(text: str) -> None:
            nonlocal head_noise
            text = strip_shell_markers(text, session.shell_markers)
            if head_noise:  # ``bash -lic`` startup warnings, as the pipe reader strips them
                text = self._clean_shell_noise(text)
                head_noise = not text.strip()
            self._ingest_output(session, text)

        def running() -> bool:
            if session._reader_finish_requested.is_set():
                return False
            if proc is not None:
                return proc.poll() is None
            return self._detached_host_fate(session.pid, session.host_start_time) == "running"
        try:
            with open(session.output_log, "rb") as stream:
                drained = 0
                while True:
                    # Decided BEFORE the read: once the process is gone every byte it wrote is readable.
                    alive = running()
                    chunk = stream.read(65536)
                    if chunk:
                        if text := decoder.decode(chunk):
                            ingest(text)
                        drained += 0 if alive else len(chunk)
                        if drained < _FINAL_DRAIN_MAX_BYTES:
                            continue
                    if not alive:
                        break
                    time.sleep(_TAIL_SECONDS)
        except OSError as exc:
            logger.warning("Process output log %s unreadable: %s", session.output_log, exc)
        if proc is not None:
            self._finish_reader(session, decoder, ingest, "Log", proc.wait, lambda: proc.returncode)
            return
        if tail := decoder.decode(b"", final=True):
            ingest(tail)
        code = recorded_exit_code(session.output_log)
        if code is None:
            # SIGKILL / exec past the trap: the spawner is gone and no status was recorded.
            self._finish_exited(session, -1, reason="lost", source="backend_lost")
        else:
            self._finish_exited(session, code)
