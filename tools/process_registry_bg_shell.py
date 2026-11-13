"""Login-shell argv/env for background commands (spawn_local).

The user's login shell loads the rc files (#42203), then ``exec``s the same bash the foreground
terminal uses, so word splitting, ``PIPESTATUS`` etc. match between foreground and background.
The command text rides in ``HERMES_BG_COMMAND`` (set on the spawn env), never re-quoted into argv.

Whatever the rc files print (Ubuntu's sudo hint, motd, locale warnings) is not the command's
output. Just before the ``exec`` the login shell prints a per-job start marker on its own line;
the readers hold everything until that line and drop it with the marker (``gate_start_output``),
so poll/log/the dock/watch patterns see only the command's own output. [daveey]
"""

import re
import shlex
import uuid

from tools.environments.local import _IS_WINDOWS, _find_bash, _find_shell

# Pre-marker text held before giving up on the marker (an rc file that floods): release all but a
# tail that could still begin the marker line, and keep looking.
START_HOLD_MAX_CHARS = 1 << 16
# bash's job-control warnings under ``-lic`` without a tty (jobs whose argv prints no start marker).
SHELL_NOISE_SUBSTRINGS = (
    "no job control in this shell", "cannot set terminal process group",
    "tcsetattr: Inappropriate ioctl for device")


def clean_shell_noise(text: str) -> str:
    """Strip shell startup warnings from the beginning of output."""
    lines = text.split("\n")
    while lines and any(noise in lines[0] for noise in SHELL_NOISE_SUBSTRINGS):
        lines.pop(0)
    return "\n".join(lines)


def new_start_marker() -> str:
    return f"__hermes_bg_start_{uuid.uuid4().hex}__"


def bg_login_argv(command: str, start_marker: str = "") -> list[str]:
    if _IS_WINDOWS:  # unchanged: Windows keeps the command in argv (and prints no start marker)
        return [_find_shell(), "-lic", f"set +m; {command}"]
    # The leading newline ends an unterminated rc line, so the marker always starts its own line.
    start = f"printf '\\n%s\\n' {shlex.quote(start_marker)}; " if start_marker else ""
    return [_find_shell(), "-lic",
            'set +m; _hc=$HERMES_BG_COMMAND; unset HERMES_BG_COMMAND; '
            f'{start}exec {shlex.quote(_find_bash())} -c "$_hc"']


def bg_command_env(env: dict, command: str) -> dict:
    """Add the command text the login shell hands to bash."""
    if not _IS_WINDOWS:
        env["HERMES_BG_COMMAND"] = command
    return env


def arm_start_gate(session, argv: list, marker: str) -> None:
    """Hold the job's output until ``marker``, only when ``argv`` actually prints it (not on Windows,
    nor for a replaced argv), so a job is never held for a marker that cannot come."""
    session.start_marker = marker if marker and any(marker in str(a) for a in argv) else ""
    session._start_held = "" if session.start_marker else None


def gate_start_output(session, text: str) -> str:
    """The part of ``text`` to ingest: nothing before the marker line (held), what follows it after."""
    held = session._start_held
    if held is None:
        return text
    buf = held + text
    found = re.search(rf"\r?\n{re.escape(session.start_marker)}\r?\n", buf)
    if found:
        session._start_held = None
        return buf[found.end():]
    if len(buf) <= START_HOLD_MAX_CHARS:
        session._start_held = buf
        return ""
    keep = len(session.start_marker) + 3  # "\r\n" + marker + "\r": a marker line not yet complete
    session._start_held = buf[-keep:]
    return buf[:-keep]


def release_start_output(session) -> str:
    """At the end of the output with no marker seen (an rc file exited, the shell failed): the held
    text is all the job printed, so it is shown as is."""
    held, session._start_held = session._start_held, None
    return held or ""
