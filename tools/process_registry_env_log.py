"""Incremental sandbox log reads and the poller loop for ProcessRegistry's non-local backends."""

import logging
import re
import shlex
import time
from typing import Any, NamedTuple

logger = logging.getLogger("tools.process_registry")

_HEADER = re.compile(r"([0-9]{1,20}) ([0-9]{1,20}) (?:(run)|exit:([0-9]{0,9}))")


class LogDelta(NamedTuple):
    """One parsed ``log_delta_command`` reply. ``exit_code`` is ``None`` while the
    process runs; once it has exited it is the recorded code, or -1 when the
    wrapper recorded none."""
    size: int
    offset: int
    exit_code: int | None
    delta: str


def log_delta_command(quoted_log_path: str, offset: int, quoted_pid_path: str, quoted_exit_path: str) -> str:
    """Shell command that reads only the log bytes written since ``offset``
    (``cat``-ing the whole file every poll re-sends all output over docker/SSH)
    and reports the process status in the same exec (sandboxes rate-limit exec
    starts, so one poll must cost one).

    Prints one header line ``"<size> <offset> <status>"`` then the bytes in
    [offset, size). The status is ``run`` while ``kill -0`` on the pid file
    succeeds, else ``exit:<code>`` (``exit:`` with no digits when the exit file
    is missing or unreadable). It sits in the header line, ahead of the delta, so
    log content cannot forge it.

    The status is taken BEFORE the size: the wrapper shell writes the exit file
    only after the command it wraps has returned, so once the wrapper is gone its
    output is in the log, and the delta in that reply is the last of it.
    The size is read before the tail and the tail cut at that same size, so a
    growing file never sends a byte twice; a file that shrank was rotated or
    truncated, so the offset drops to 0 and the reader starts over.

    While the process runs, the window end is pulled back to a UTF-8 character
    boundary (the backend decodes each ``execute()`` result on its own, so a
    straddling multibyte char would become U+FFFD and break watch patterns at the
    seam): up to 3 trailing continuation bytes are held for the next poll and the
    header reports the trimmed size. After an exit no poll follows, so nothing is
    held back and a truncated trailing sequence is delivered (decoded as U+FFFD)."""
    return (
        f'if kill -0 "$(cat {quoted_pid_path} 2>/dev/null)" 2>/dev/null; then T=run; '
        f"else T=\"exit:$(cat {quoted_exit_path} 2>/dev/null | tr -dc '0-9')\"; fi; "
        f"O={offset}; "
        f"S=$({{ wc -c < {quoted_log_path}; }} 2>/dev/null | tr -dc '0-9'); "
        f"S=${{S:-0}}; "
        f'if [ "$S" -lt "$O" ]; then O=0; fi; '
        f'case "$T" in exit:*) ;; *) '
        # Scan back up to 3 continuation bytes (octal 200-277) to the lead byte; if
        # the lead's declared length (3xx=2, 34x-35x=3, 36x-37x=4) exceeds the bytes
        # present, trim to before it. Complete sequences and ASCII tails untouched.
        f'N=0; P=$S; while [ "$P" -gt "$O" ] && [ "$N" -lt 3 ]; do '
        f"B=$(tail -c +$P {quoted_log_path} 2>/dev/null | head -c 1 | od -An -to1 | tr -dc '0-9'); "
        f'case "$B" in 2[0-7][0-7]) P=$((P-1)); N=$((N+1));; *) break;; esac; done; '
        f'if [ "$N" -gt 0 ] || [ "$P" -eq "$S" ]; then '
        f"B=$(tail -c +$P {quoted_log_path} 2>/dev/null | head -c 1 | od -An -to1 | tr -dc '0-9'); "
        f'case "$B" in 3[0-3][0-7]) L=2;; 3[4-5][0-7]) L=3;; 3[6-7][0-7]) L=4;; *) L=1;; esac; '
        f'if [ "$L" -gt $((N+1)) ]; then S=$((P-1)); fi; fi; '
        f"esac; "
        f'echo "$S $O $T"; '
        f'if [ "$S" -gt "$O" ]; then '
        f"tail -c +$((O+1)) {quoted_log_path} 2>/dev/null | head -c $((S-O)); fi"
    )
def parse_log_delta_reply(raw: str) -> LogDelta | None:
    """Parse a ``log_delta_command`` reply. ``None`` when it has no usable header
    (command failed, shell missing a tool): the caller drops that reply rather
    than act on a half-read value."""
    header, _, delta = raw.partition("\n")
    # ASCII digits only (``\d`` and ``str.isdigit`` accept other scripts), bounded
    # so ``int()`` cannot hit the interpreter's digit limit on a mangled reply.
    match = _HEADER.fullmatch(header.strip())
    if match is None:
        return None
    size, offset, running, code = match.groups()
    exit_code = None if running else int(code) if code else -1
    return LogDelta(int(size), int(offset), exit_code, delta)


def fallback_exit_code(env: Any, quoted_pid_path: str, quoted_exit_path: str) -> int | None:
    """Separate liveness and exit-code reads for a reply the combined command
    could not deliver, so a backend that mangles it still sees the exit.
    ``None`` while the process looks alive, else the recorded code (-1 if none)."""
    check = env.execute(
        f"kill -0 \"$(cat {quoted_pid_path} 2>/dev/null)\" 2>/dev/null; echo $?", timeout=5)
    check_output = check.get("output", "").strip()
    if not check_output or check_output.splitlines()[-1].strip() == "0":
        return None
    exit_str = env.execute(f"cat {quoted_exit_path} 2>/dev/null", timeout=5).get("output", "").strip()
    try:
        return int(exit_str.splitlines()[-1].strip())
    except (ValueError, IndexError):
        return -1


def env_poller_loop(registry, session, env, log_path: str, pid_path: str, exit_path: str) -> None:
    """Background thread: poll a sandbox log file for non-local backends (``ProcessRegistry._env_poller_loop``).

    One ``execute()`` per tick: the read command also reports the process status
    (see ``log_delta_command``). Sandboxes rate-limit exec starts."""
    q = shlex.quote
    # Byte offset already read from the log (bytes, not chars: the shell counts bytes).
    prev_output_bytes = 0
    reattaches = 0  # consecutive handle re-attaches without a successful poll in between
    while not session.exited:
        time.sleep(2)
        try:
            # Read only the bytes written since the last poll, plus the process status.
            raw = env.execute(
                registry._log_delta_command(q(log_path), prev_output_bytes, q(pid_path), q(exit_path)),
                timeout=10).get("output", "")
            reply = parse_log_delta_reply(raw)
            delta = reply.delta if reply else ""
            if reply:
                if reply.offset < prev_output_bytes:
                    # Log rotated/truncated: what we hold no longer lines up. Restart.
                    with session._lock:
                        if session.exited:
                            return
                        session.output_buffer = ""
                prev_output_bytes = reply.size
            if delta:
                # A kill that landed after this fetch already published its output
                # snapshot: leave the output as the kill reported it (as the PTY reader does).
                with session._lock:
                    if session.exited:
                        return
                    session.output_buffer += delta
                    if len(session.output_buffer) > session.max_output_chars:
                        session.output_buffer = session.output_buffer[-session.max_output_chars:]
                registry._check_watch_patterns(session, delta)
                registry._emit_output(session, delta)

            # Unreadable reply: separate liveness and exit-code reads, as before the combined read.
            exit_code = reply.exit_code if reply else fallback_exit_code(env, q(pid_path), q(exit_path))
            if exit_code is not None:
                registry._finish_exited(session, exit_code)
                return
        except Exception as exc:  # health: allow BLE001 -- any backend failure: re-attach below or report lost
            logger.debug("Poller of %s: environment call failed", session.id, exc_info=True)
            # The environment handle died (idle reap, teardown, sandbox reaped, ...). A sandbox that is
            # still up keeps the job running: re-attach through the recorded handle and keep watching,
            # so the session gets the real exit instead of a false "lost". [daveey]
            reattached = reattach_poller_env(session, pid_path, exc) if reattaches < 3 else None
            if reattached is not None:
                env, reattaches = reattached, reattaches + 1
                continue
            # A kill that already settled the session keeps its result.
            registry._finish_exited(session, -1, reason="lost", source="backend_lost")
            return
        reattaches = 0


def reattach_poller_env(session, pid_path: str, exc: BaseException) -> Any:
    """A fresh ``execute()`` handle on *session*'s still-running sandbox, or None. [daveey]

    Only when the spawn recorded a re-attach handle, the sandbox is running, it did not restart
    after the job started, and the job's wrapper PID file is still there."""
    if session.exited or not session.sandbox_handle:
        return None
    from tools.environments import reattach as _reattach
    try:
        env = _reattach.reattach_environment(session.sandbox_handle)
        if env is None:
            return None
        started = getattr(env, "started_at", None)
        if isinstance(started, (int, float)) and isinstance(session.started_at, (int, float)) \
                and started > session.started_at + 5:
            return None
        probe = env.execute(f"test -e {shlex.quote(pid_path)} && echo hermes-bg-present", timeout=10)
        if "hermes-bg-present" not in str(probe.get("output", "")):
            return None
    except Exception:  # health: allow BLE001 -- any failure means "cannot re-attach"; logged with traceback
        logger.debug("Poller re-attach of %s failed", session.id, exc_info=True)
        return None
    session.env_ref = env
    logger.info("Re-attached sandbox process %s (pid=%s) of session %s after its environment handle failed "
                "(%s): %s", session.id, session.pid, session.session_key or session.owner_task_id or "-",
                str(exc)[:120], session.command[:60])
    return env
