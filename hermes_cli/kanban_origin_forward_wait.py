"""Refuse a park that waits on a product key this run's own creation definitively lost.

``kanban_create`` returns a queued ``wait-check key`` handle before the origin gateway has
judged the forward.  A later ``origin_forward_binding_missing`` rejection happens before any
owner create, so no card with that key exists or ever will from that request, and the probe
would sleep to its backstop.  ``origin_forward_run_end`` deliberately lets that definitive
refusal end the run; this module only stops the creator from *waiting* on the lost child.

Read-only evidence, never authority: nothing here inherits, mints or rebinds an origin
binding, edits a frozen request, or treats a replica as an accepted receipt.  Missing,
unreadable or ambiguous courier evidence is "cannot tell", never a refusal.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
import shlex
import sqlite3
import string
from typing import Optional

CODE = "origin_forward_rejected_wait"
_REFUSED = "origin_forward_binding_missing"


_REDIRECT = re.compile(r"^(?:[<>&]*[<>][<>&]*|>\|)$")  # changes no exit status: ``> f``, ``2>&1``, ``&> f``, ``>| f``
_SEQUENCE = {";", ";;", "\n"}  # the probe fires on the exit status of its last command
_SYNTAX = "();<>|&'\"\\"
# An escaped backslash pairs off first, so only a backslash that is itself unescaped continues the line.
# (bash: backslash-CR is an escaped CR, not a continuation.)
_CONTINUATION = re.compile(r"(\\\\)|\\\n")


class _WordChars(str):
    """Every character that is neither whitespace nor shell syntax, whatever its script.

    shlex only asks ``char in wordchars``; a printable-ASCII table would end an unquoted word
    (``café-1``) at its first non-ASCII character.
    """

    def __contains__(self, char) -> bool:  # type: ignore[override]
        return isinstance(char, str) and len(char) == 1 and char not in string.whitespace and char not in _SYNTAX


def _words(wake_check: str) -> list[str]:
    """Shell words of ``wake_check`` with every operator its own word, however it is spaced."""
    wake_check = _CONTINUATION.sub(lambda m: m.group(1) or " ", wake_check)  # a continuation is a space
    lexer = shlex.shlex(wake_check, posix=True, punctuation_chars=True)
    lexer.whitespace = " \t\r"  # a newline separates commands: keep it as a word
    lexer.wordchars = _WordChars()  # keys like ``bb-land:817:ab`` and ``café-1`` stay one word
    lexer.commenters = ""
    return list(lexer)


def wait_keys(wake_check: Optional[str]) -> list[str]:
    """Every product key the probe's exit status depends on, from ``hermes kanban wait-check key <KEY>``.

    ``a && b`` fires only when both do, so every key of the final command sequence is judged
    (``;`` and newline: only the last command decides).  Anything that lets the probe fire without
    one of its keys (``||``, ``|``, ``&``, subshells) is "cannot tell" and yields no keys.
    Operators are recognised with or without surrounding spaces.
    """
    try:
        words = _words(wake_check or "")
    except ValueError:
        return []
    commands: list[list[str]] = [[]]
    for word in words:
        if word == "\n" and (not commands[-1] or commands[-1][-1] == "&&"):
            continue  # a blank line, or a line break after ``&&``, continues the command
        if word in _SEQUENCE:
            commands.append([])
        elif word == "&&" or _REDIRECT.match(word):
            commands[-1].append(word)
        elif word and set(word) <= set("();<>|&"):
            return []  # ``||``, ``|``, ``&``, ``(``, ``)``: the probe can fire without the key
        else:
            commands[-1].append(word)
    last = next((c for c in reversed(commands) if c), [])  # a trailing ``;`` or blank line decides nothing
    keys = []
    for i in range(len(last) - 4):
        if last[i:i + 3] == ["hermes", "kanban", "wait-check"] and last[i + 3] == "key":
            key = last[i + 4]
            if key and not key.startswith("-"):
                keys.append(key)
    return keys


def _run_uid(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    from hermes_cli import kanban_db_sync as sync
    if "uid" not in sync.table_columns(conn, "task_runs"):
        return None  # unsynced boards carry no portable run identity, as in require_settled
    row = conn.execute("SELECT r.uid FROM tasks t JOIN task_runs r ON r.id=t.current_run_id "
                       "WHERE t.id=? AND r.task_id=t.id AND r.ended_at IS NULL", (task_id,)).fetchone()
    return row[0] if row and isinstance(row[0], str) and row[0] else None


def _attempts(task_id: str, key: str) -> Optional[tuple[list, list]]:
    """``(request id, state, result, run uid)`` per courier row creating ``key``: this creator's, then others'."""
    from hermes_cli.kanban_capability_registry import hub_db_path, _ro
    try:
        path = hub_db_path()
        path.lstat()
        with closing(_ro(path)) as db:
            rows = db.execute("SELECT id,state,result,body FROM sent WHERE body LIKE '%origin-forward%'").fetchall()
    except (sqlite3.Error, OSError):
        return None
    found, others = [], []
    for rid, state, raw_result, raw_body in rows:
        try:
            body = json.loads(raw_body)
            args = body["args"]
            if (body.get("coll") != "sessions" or body.get("action") != "origin-forward"
                    or args["create"]["idempotency_key"] != key):
                continue
            mine = args["creator"]["task_id"] == task_id
            run_uid = args.get("run_uid")
        except (ValueError, TypeError, KeyError, AttributeError):
            continue  # a row we cannot attribute is neither this creator's nor evidence
        try:
            result = json.loads(raw_result or "{}")
        except (ValueError, TypeError):
            result = None
        (found if mine else others).append((str(rid), state, result if isinstance(result, dict) else {}, run_uid))
    return found, others


def _definitive(state, result: dict) -> bool:
    """The exact pre-create refusal ``kanban_placement._forward_binding_retry`` also recognizes."""
    return (state == "rejected" and result.get("code") == _REFUSED
            and result.get("status") == "rejected" and result.get("handoff_complete") is False)


def _lost(task_id: str, key: str, run_uid: str) -> Optional[str]:
    """The request id of this run's definitively refused creation of ``key``, else None."""
    evidence = _attempts(task_id, key)
    if evidence is None:
        return None
    attempts, others = evidence
    if not attempts or not all(_definitive(state, result) for _, state, result, _ in attempts + others):
        # No evidence, a recovery/in-flight/done request (even another creator's: the key may
        # exist), or another failure: not this gate.
        return None
    mine = [rid for rid, _, _, uid in attempts if uid == run_uid]
    return sorted(mine)[0] if mine else None  # none: an earlier run's refusal, possibly recovered since


def rejected_wait(conn: sqlite3.Connection, task_id: str, wake_check: Optional[str]) -> Optional[str]:
    """Refusal text when this park waits on a key whose creation this run definitively failed."""
    keys = wait_keys(wake_check)
    if not keys:
        return None
    from hermes_cli.kanban_workspace_publication import _kernel_park
    if _kernel_park.get() == task_id:
        return None  # mandatory kernel parks stay possible, as for the pending gate
    run_uid = _run_uid(conn, task_id)
    if not run_uid:
        return None
    for key in keys:
        rid = _lost(task_id, key, run_uid)
        if rid:
            return (f"{CODE}: this run's creation of the child with idempotency key {key} (request "
                    f"{rid}) was definitively refused as {_REFUSED}, before any card was created, so "
                    "`wait-check key` can never see it. Do not park on it and do not retry creation in this run. "
                    "End the run reporting the lost child (kanban_complete or kanban_block with the refusal; the "
                    "owning chat already holds the rejection notice), or, if the owning chat created the card "
                    "another way, wait on that card's id instead. No origin binding was inherited, minted or changed.")
    return None
