"""A note on a foreground ``sleep``/poll command that had something better to wait on. [daveey]

Measured on the hub over 7 days: 52 h of agent time in 2,838 terminal calls of the shape
``sleep 540; tail run.log`` or ``sleep 150; hermes kanban show t_...``. Half waited on a background
process the same session had started (``process_manage(action="wait")`` returns the moment it exits);
28% re-read the status of another card (a Kanban worker can park on that card).

This only ADDS a ``wait_hint`` field to the result of a foreground command that ran to an exit
status. It never refuses or rewrites a command, and its own work is linear in the command length
(longer commands are skipped), so a wrong match costs one sentence, not a lost or late result.

A hint is only given for a command whose own execution held the call for half a minute or more
(the successful ``env.execute`` attempt, timed in ``_run_foreground``; not the text, and not the
approval wait, sandbox start or retries around it), and the process hint carries ``timeout=<that measured
time>``: following it never blocks longer than the command it replaces, even when the named process
turns out to be a server that never exits. Beyond that it prefers silence to a wrong hint. No hint for: a result that is not a plain finished command
(refused, errored, degraded, yielded, or turned into a background process), a sleep followed by a
look at something outside this machine's own job (a service probe, CI, a remote host), a process
that is a server or already notifies on exit, and a worker reading its own card. Shell text that is
data (quotes, heredoc bodies, comments) is not read as commands.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

# Commands longer than this are not examined: the scan below is linear, this bounds it outright.
MAX_COMMAND_CHARS = 8000
# Shortest single sleep worth a note; anything shorter is a settle delay, not a wait.
MIN_SLEEP_SECONDS = 60
# Shortest sleep inside a shell loop that makes the loop a poll loop.
MIN_LOOP_SLEEP_SECONDS = 5
# A command that returned faster than this did not wait on anything (a readiness loop that hit at
# once, a sleep in an untaken branch or a function body): no hint.
MIN_ELAPSED_SECONDS = 30
_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}

_HEREDOC_START_RE = re.compile(r"<<-?[ \t]*\\?['\"]?([^\s'\"|;&<>()]+)")
_SEGMENT_SPLIT_RE = re.compile(r"[;&|\n(){}]+")
_BACKGROUNDED_RE = re.compile(r"\s*&(?!&)")  # the separator after a command is a lone `&`
_SLEEP_SEGMENT_RE = re.compile(r"(?:(?:do|then|else)\s+)*sleep\s+(\d{1,7}(?:\.\d{1,3})?)([smh]?)(?:\s|$)")
_LOOP_START_RE = re.compile(r"(?:while|until|for)\b")
_DO_RE = re.compile(r"do(?:\s|$)")
_DONE_RE = re.compile(r"done\b")

# What the command looks at after sleeping is not this session's own job: a listening service, CI,
# another host. A wait on the running process would be the wrong advice (often it is the server).
_ELSEWHERE_RE = re.compile(
    r"\b(?:curl|wget|nc|ncat|http|https|pg_isready|redis-cli|lsof|gh|docker|kubectl|ssh|aws|gcloud|systemctl|launchctl)\b"
    r"|\blocalhost\b|\b127\.0\.0\.1\b|\b0\.0\.0\.0\b|https?://|\bss\s+-")
# A background command that serves or watches until killed: waiting for it to exit never returns.
_SERVER_COMMAND_RE = re.compile(
    r"\b(?:serve|server|runserver|uvicorn|gunicorn|hypercorn|vite|webpack|nodemon|watch|tail\s+-[a-zA-Z]*[fF])\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|preview)\b|http\.server|--watch\b")

# `kanban [--flag [value]]... show <id>` as a command word in code (not in a quoted string, not a path
# component). Each flag token matches one way only, so the cost is linear; an earlier pattern that
# let a flag match two ways backtracked exponentially.
_CARD_SHOW_RE = re.compile(r"(?<![\w/.-])kanban\s+(?:--[\w-]+(?:=\S+|\s+[^\s-]\S*)?\s+)*show\s+(t_[0-9a-f]{8,})\b")
# sqlite3 on a board database with a statement that STARTS with select and filters on id = <card>.
_CARD_QUERY_RE = re.compile(
    r"\bsqlite3\b[^;|&\n]{0,200}?(?:kanban\.db|sync-shadow/[\w.-]{1,80}\.db)(?![\w.])\s+[\"']\s*select\b"
    r"[^;|&\n]{0,400}?(?<![\w.])id\s*=\s*[\"']?(t_[0-9a-f]{8,})\b", re.IGNORECASE)


_measured = threading.local()


def note_execute_seconds(seconds: float) -> None:
    """Record how long the foreground command of the current terminal call ran (same thread)."""
    _measured.seconds = seconds


def take_execute_seconds() -> float | None:
    """The recorded run time of this thread's last foreground command, cleared on read; ``None``
    when no command ran to completion (refused, failed, backgrounded) since the last read."""
    seconds = getattr(_measured, "seconds", None)
    _measured.seconds = None
    return seconds


def _held_the_call(elapsed_seconds) -> bool:
    return (isinstance(elapsed_seconds, (int, float)) and not isinstance(elapsed_seconds, bool)
            and elapsed_seconds >= MIN_ELAPSED_SECONDS)


def _drop_heredoc_bodies(command: str) -> str:
    """``command`` without heredoc bodies (their lines are data). A ``<<`` that is not a heredoc may
    swallow the rest of the command, which only makes the scan see less."""
    kept, pending = [], []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        kept.append(line)
        pending = _HEREDOC_START_RE.findall(line)
    return "\n".join(kept)


def _skip_group(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the group opened at ``start`` (nesting counted); end of text if unclosed."""
    depth, i = 0, start
    while i < len(text):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _code_only(command: str) -> str:
    """Shell text with data blanked, in one pass: quoted strings (``'..'``, ``".."``, ``$'..'``) and
    expansions (``${..}``, ``$(..)``) become one opaque word; comments and heredoc bodies are removed."""
    out, i, text = [], 0, _drop_heredoc_bodies(command)
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch == "\\":
            i += 2  # an escaped character is never a quote, separator or comment start
            continue
        if ch == "$" and nxt in "{(":
            i = _skip_group(text, i + 1, nxt, "}" if nxt == "{" else ")")
            out.append(" V ")
            continue
        if ch in "'\"" or (ch == "$" and nxt == "'"):
            ansi_c = ch == "$"
            quote = "'" if ansi_c else ch
            i += 2 if ansi_c else 1
            while i < len(text) and text[i] != quote:
                if quote == '"' and text.startswith("$(", i):
                    i = _skip_group(text, i + 1, "(", ")")  # a substitution's own quotes do not close ours
                else:
                    # Backslash escapes exist inside ".." and $'..', not inside plain '..'.
                    i += 2 if text[i] == "\\" and (ansi_c or quote == '"') else 1
            i += 1
            out.append(" Q ")  # the string stays one opaque word, so the text around it is not joined
            continue
        if ch == "#" and (not out or out[-1][-1].isspace() or out[-1][-1] in ";&|(){}"):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _sleeps(code: str) -> list[tuple[float, bool]]:
    """``(seconds, inside a loop body)`` for each foreground ``sleep N`` that is a command of its own
    (a sleep followed by a lone ``&`` runs in the background and does not hold the call)."""
    found, depth, pos = [], 0, 0
    for sep in [*_SEGMENT_SPLIT_RE.finditer(code), None]:
        segment = code[pos:sep.start() if sep else len(code)].strip()
        backgrounded = bool(sep and _BACKGROUNDED_RE.match(sep.group()))
        pos = sep.end() if sep else len(code)
        if not segment:
            continue
        if _DONE_RE.match(segment):
            depth = max(0, depth - 1)
            continue
        if _DO_RE.match(segment):
            depth += 1
        elif _LOOP_START_RE.match(segment):
            continue
        m = _SLEEP_SEGMENT_RE.match(segment)
        if m and not backgrounded:
            found.append((float(m.group(1)) * _UNIT_SECONDS[m.group(2)], depth > 0))
    return found


def _wait_sleeps(command: str) -> list[tuple[float, bool]]:
    if not command or len(command) > MAX_COMMAND_CHARS:
        return []
    return [(seconds, in_loop) for seconds, in_loop in _sleeps(_code_only(command))
            if seconds >= MIN_SLEEP_SECONDS or (in_loop and seconds >= MIN_LOOP_SLEEP_SECONDS)]


def is_sleep_wait(command: str) -> bool:
    """True for a command that waits by sleeping: a literal foreground sleep of a minute or more, or
    one of 5 s or more inside a shell loop body. Quotes, expansions, heredocs and comments do not count."""
    return bool(_wait_sleeps(command))


def awaited_card_id(command: str, *, own_card: str | None = None) -> str | None:
    """The one card whose status the command reads (``kanban show <id>`` in code, or a board DB
    ``select ... id = <id>``), if it is not ``own_card``."""
    command = command or ""
    if len(command) > MAX_COMMAND_CHARS:
        return None
    ids = {*_CARD_SHOW_RE.findall(_code_only(command)), *_CARD_QUERY_RE.findall(command)}
    if len(ids) != 1:
        return None
    card = ids.pop()
    return None if card == (own_card or "").strip() else card


def _running_processes(task_id: str | None) -> list:
    if not task_id:
        return []
    try:
        from tools.process_registry import process_registry
        return process_registry.running_owned_by(task_id)
    except Exception:
        logger.debug("wait hint: process lookup failed", exc_info=True)
        return []


def _waitable_process(command: str, task_id: str | None):
    """The newest running job of this task that a wait would help with, or ``None``: none when the
    command looks elsewhere after sleeping, and never a server or a process that notifies on exit."""
    if _ELSEWHERE_RE.search(command):
        return None
    candidates = [p for p in _running_processes(task_id)
                  if not getattr(p, "notify_on_complete", False)
                  and not _SERVER_COMMAND_RE.search(getattr(p, "command", "") or "")]
    return max(candidates, key=lambda p: getattr(p, "started_at", 0) or 0) if candidates else None


def _shown_command(proc) -> str:
    """The process command, redacted BEFORE it is shortened (a cut token no longer matches)."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(getattr(proc, "command", "") or "", code_file=True)[:80]
    except Exception:
        logger.debug("wait hint: could not redact the process command; showing none", exc_info=True)
        return ""


def wait_hint(command: str, task_id: str | None, elapsed_seconds: float | None, *,
              kanban_task: str | None = None) -> str | None:
    """The note for ``command``, which took ``elapsed_seconds``, or ``None`` when it is not a
    sleep-wait, returned quickly, or nothing better exists. ``kanban_task`` is this worker's own
    card ("" for a chat); default: ``HERMES_KANBAN_TASK``."""
    if not _held_the_call(elapsed_seconds) or not is_sleep_wait(command):
        return None
    if kanban_task is None:
        kanban_task = os.environ.get("HERMES_KANBAN_TASK") or ""
    kanban_task = kanban_task.strip()
    parts = []
    proc = _waitable_process(command, task_id)
    if proc is not None:
        seconds = int(min(elapsed_seconds, 86400))
        parts.append(
            f"Background process {proc.id} ({_shown_command(proc)}) is still running. If this command was "
            f"waiting for it to finish, process_manage(action='wait', session_id='{proc.id}', timeout={seconds}) "
            f"waits no longer than this command ran ({seconds}s) but returns the moment the process exits, "
            "with its output; use that instead of sleeping and reading its log.")
    card = awaited_card_id(command, own_card=kanban_task)
    if card:
        if kanban_task:
            parts.append(
                f"To wait on card {card}, park this card instead of sleeping: kanban_schedule or kanban_block "
                f"with awaiting={{'kind': 'card', 'ref': '{card}'}} (add gateway=<name> if it lives on another "
                "gateway). This card wakes when that one finishes, and the worker slot is free meanwhile.")
        else:
            parts.append(
                f"If this chat created card {card} (kanban_create), its result is delivered here when it "
                "finishes: end the turn instead of sleeping and re-reading its status.")
    return " ".join(parts) or None


# Keys that mark a result as something other than a foreground command that ran to an exit status:
# an outcome label, or a command the tool turned into a tracked background process (a trailing `&`,
# a timeout above the foreground cap) - its own sleep is then the "running process".
_NOT_A_FINISHED_COMMAND_KEYS = ("status", "session_id", "pid", "auto_backgrounded", "promoted_from_foreground", "wait_hint")


def annotate(result: str, command, *, background: bool, task_id: str | None, elapsed_seconds: float | None) -> str:
    """``result`` (the terminal tool's JSON) with ``wait_hint`` added when one applies.
    ``elapsed_seconds`` is how long the command itself ran (``take_execute_seconds``). Unchanged for a background call, a non-string
    command, a non-JSON result, and any result that is not a foreground command that finished
    cleanly: only ``exit_code == 0`` with no ``error``, no ``status`` and no sign of a spawn counts.
    (A non-zero status is also what a cut-short command, an interrupt and some backend failures
    report, and they cannot be told apart here.)"""
    if background or not isinstance(command, str) or not isinstance(result, str):
        return result
    try:
        if not _held_the_call(elapsed_seconds) or not is_sleep_wait(command):
            return result
        data = json.loads(result)
        if not isinstance(data, dict) or any(key in data for key in _NOT_A_FINISHED_COMMAND_KEYS):
            return result
        exit_code = data.get("exit_code")
        if isinstance(exit_code, bool) or exit_code != 0 or not isinstance(exit_code, int) or data.get("error"):
            return result
        hint = wait_hint(command, task_id, elapsed_seconds)
        if not hint:
            return result
        data["wait_hint"] = hint
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        logger.debug("wait hint skipped", exc_info=True)
        return result  # a hint must never cost the caller its result
