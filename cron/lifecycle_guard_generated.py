"""Scripts a command writes before it runs them, for the lifecycle guard (t_01a1d88e9c759e09).

``cat > F <<'EOF' … EOF; bash F`` runs the heredoc body, but the referenced-script walk reads F
from disk: missing (nothing to scan) or stale (the old content) at scan time. Every write the
command makes with an output redirection (``>``, ``>>``, ``>|``, ``&>``, ``>&F``) or ``tee`` is
recorded here with what it writes, and the walk scans that text wherever it runs the path:

- ``cat``/``tee`` fed by a heredoc or here-string: the body (a quoted heredoc verbatim; an
  unquoted one whose body holds a substitution writes output nobody can predict, so unknown);
- ``echo``/``printf`` with literal arguments: the lines they may print (escapes decoded);
- any other writer (``curl … > F``, ``python3 gen.py > F``, ``cat a b > F``, stderr, ``3<> F``):
  unknown (``<> F`` on stdin is input, as in ``bash <> F``). Executing a path with an unknown
  write fails closed; mentioning it does not, and a Python interpreter's file never fails closed
  (as everywhere in the walk).

The text on disk is still read and scanned too (an append keeps it, and the scan is a superset).
Writes are matched by path, not by order or control flow, and ``cd`` is not tracked, the same
lexical model the rest of the walk uses.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from cron.lifecycle_guard import (
    _REDIRECTION,
    _executable_name,
    _executed_command_index,
    _iter_command_segment_views,
    _on_cloud_path,
    _printed_argument_lines,
    _refuse_unreadable,
    _resolve_lenient,
    _resolved_or_nothing,
    _with_escapes_decoded,
)
from tools.shell_heredoc import _parse_heredoc_operator, iter_heredoc_bodies

if TYPE_CHECKING:
    from cron.lifecycle_guard import _LifecycleScanBudget

# Marks a write whose content the guard cannot know.
UNKNOWN: None = None
# Text that makes a heredoc body or argument produce output decided only when it runs.
_DYNAMIC_MARKS = ("$(", "`", "<(", ">(")
_STDIN_COPIERS = frozenset({"cat", "tee"})
_PRINTERS = frozenset({"echo", "printf"})


def _owner_has_unquoted_heredoc(owner: str) -> bool:
    """Whether some ``<<`` opener in *owner* has an unquoted delimiter (its body expands)."""
    position = owner.find("<<")
    while position >= 0:
        if not owner.startswith("<<<", position):
            parsed = _parse_heredoc_operator(owner, position)
            if parsed is None or not parsed[3]:
                return True
            position = parsed[0]
        else:
            position += 3
        position = owner.find("<<", position)
    return False


def _bodies_by_view(command: str) -> dict[tuple[str, ...], list[Optional[str]]]:
    """Heredoc bodies keyed by every segment view of their owner that opens a heredoc. An owner
    the parser cannot name, or an unquoted body that runs a substitution, maps to UNKNOWN."""
    mapped: dict[tuple[str, ...], list[Optional[str]]] = {}
    for owner, body in iter_heredoc_bodies(command):
        if owner is None:
            continue  # its writer segment finds no body below: an unknown write
        dynamic = _owner_has_unquoted_heredoc(owner) and any(m in body for m in _DYNAMIC_MARKS)
        for view in _iter_command_segment_views(owner):
            if any(_opens_heredoc(word) for word in view):
                mapped.setdefault(tuple(view), []).append(UNKNOWN if dynamic else body)
    return mapped


def _opens_heredoc(word: str) -> bool:
    match = _REDIRECTION.match(word)
    return match is not None and match.group(2) in ("<<", "<<-")


def _writes(view: list[str], index: int) -> tuple[list[str], list[str], dict]:
    """``(stdout targets, other written targets, inputs)`` of the command at *index*."""
    stdout: list[str] = []
    other: list[str] = []
    inputs = {"heredoc": False, "herestrings": [], "stdin_file": False, "operands": []}
    words = iter(view[index + 1:])
    for word in words:
        match = _REDIRECTION.match(word)
        if match is None:
            inputs["operands"].append(word)
            continue
        fd, operator, target = match.groups()
        target = target or next(words, "")
        if operator in ("<<", "<<-"):
            inputs["heredoc"] = True
        elif operator == "<<<":
            inputs["herestrings"].append(target)
        elif operator in (">&", "<&") and (target.isdigit() or target in ("-", "")):
            continue  # descriptor duplication or close: no file
        elif operator == "<" or (operator == "<>" and fd in ("", "0")):
            inputs["stdin_file"] = True  # `bash <> F` reads F as its program; nothing writes it
        elif operator == "<>" or (fd not in ("", "1") and not operator.startswith("&")):
            other.append(target)  # `2> F`, `{fd}> F`, `3<> F`: written by unpredictable output
        elif ">" in operator:
            stdout.append(target)
    return stdout, other, inputs


def _view_writes(
    view: list[str], bodies: dict[tuple[str, ...], list[Optional[str]]],
) -> Iterator[tuple[str, Optional[str]]]:
    """``(target, text or UNKNOWN)`` for each file the command in *view* writes."""
    index = _executed_command_index(view)
    if index is None:
        return
    name = _executable_name(view[index])
    stdout, other, inputs = _writes(view, index)
    targets = list(stdout)
    if name == "tee":
        targets += [word for word in inputs["operands"] if not word.startswith("-")]
    if not targets and not other:
        return
    contents: list[Optional[str]] = [UNKNOWN]
    if name in _STDIN_COPIERS:
        copied_files = name == "cat" and any(
            word != "-" and not word.startswith("-") for word in inputs["operands"])
        fed: list[Optional[str]] = []
        if inputs["heredoc"]:
            fed += bodies.get(tuple(view), [UNKNOWN])
        fed += [UNKNOWN if any(m in text for m in _DYNAMIC_MARKS) else text
                for text in inputs["herestrings"]]
        if fed and not copied_files and not inputs["stdin_file"]:
            contents = fed
    elif name in _PRINTERS and not any(
            mark in word for word in inputs["operands"] for mark in _DYNAMIC_MARKS):
        contents = [variant for line in _printed_argument_lines(name, inputs["operands"])
                    for variant in _with_escapes_decoded(line)]
    for target in targets:
        for text in contents:
            yield target, text
    for target in other:
        yield target, UNKNOWN


def note_generated_scripts(budget: _LifecycleScanBudget, command: str, cwd: Optional[str]) -> None:
    """Record in *budget* every file *command* writes and what it writes (see module docstring).
    Shared across the whole walk, so a generated script that runs another one the root command
    generated is followed too."""
    if ">" not in command and "tee" not in command:
        return  # nothing written: skip a second tokenization
    bodies = _bodies_by_view(command) if "<<" in command else {}
    for view in _iter_command_segment_views(command):
        for target, text in _view_writes(view, bodies):
            for path in _resolved_or_nothing(target, cwd):
                texts = budget.generated.setdefault(_resolve_lenient(path), [])
                if text not in texts:
                    texts.append(text)


def preflight_candidate(
    budget: _LifecycleScanBudget, visited: set[object], script_path: Path, *, executed: bool,
    python_source: bool, recurse: Callable[[str, str, str], bool],
) -> Optional[bool]:
    """Checks on a walk candidate before its file is read from disk: ``True`` unsafe, ``False``
    skip it, ``None`` read and walk the file. ``recurse(text, directory, source)`` walks one text
    in the candidate's own mode."""
    # Do not touch a FileProvider path even to discover whether the file is hydrated.
    if _on_cloud_path(script_path):
        return executed and _refuse_unreadable(
            budget, script_path,
            f"`{script_path}` lives on a cloud-synced path (iCloud Drive / "
            "~/Library/CloudStorage) that the guard refuses to open",
        )
    # A file this command writes runs what it writes, not (only) what is on disk now.
    resolved = _resolve_lenient(script_path)
    if _generated_script_is_unsafe(
            budget, visited, resolved, executed=executed, python_source=python_source,
            recurse=lambda text, source: recurse(text, str(resolved.parent), source)):
        return True
    return None


def _generated_script_is_unsafe(
    budget: _LifecycleScanBudget, visited: set[object], resolved: Path, *, executed: bool,
    python_source: bool, recurse: Callable[[str, str], bool],
) -> bool:
    """Scan what the walked command wrote to *resolved*, which it also runs or mentions.
    ``recurse(text, source)`` walks one generated text in the candidate's own mode."""
    for text in budget.generated.get(resolved, ()):
        if text is UNKNOWN:
            if executed and not python_source:
                return _refuse_unreadable(
                    budget, resolved,
                    f"`{resolved}` is written earlier in the same command by a writer whose "
                    "output the guard cannot read, then executed")
            continue
        key = ("generated", resolved, python_source,
               hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest())
        if key in visited:
            continue
        visited.add(key)
        if recurse(text, f"`{resolved}` (written by this command)"):
            return True
    return False
