"""Scripts a command writes before it runs them, for the lifecycle guard (t_01a1d88e9c759e09).

``cat > F <<'EOF' … EOF; bash F`` runs the heredoc body, but the referenced-script walk reads F
from disk: missing (nothing to scan) or stale (the old content) at scan time. Every write the
command makes with an output redirection (``>``, ``>>``, ``>|``, ``&>``, ``>&F``) or ``tee`` is
recorded here with what it writes, and the walk scans that text wherever it runs the path:

- ``cat``/``tee`` fed by a heredoc or here-string: the body (a quoted heredoc verbatim; an
  unquoted one whose body holds a substitution writes output nobody can predict, so unknown);
- ``echo``/``printf`` with literal arguments: the lines they may print (escapes decoded);
- any other writer (``curl … > F``, ``python3 gen.py > F``, ``cat a b > F``, stderr, ``3<> F``,
  a redirection with no command word such as ``(echo P) > F`` or ``exec > F`` / ``exec 3>F``,
  and the destination of ``cp``/``mv``/``ln``/``install``): unknown (``<> F`` on stdin is input,
  as in ``bash <> F``). Executing a path with an unknown
  write fails closed; mentioning it does not, and a Python interpreter's file never fails closed
  (as everywhere in the walk).

The text on disk is still read and scanned too (an append keeps it, and the scan is a superset).
Writes are matched by path, not by order or control flow, and ``cd`` is not tracked, the same
lexical model the rest of the walk uses.
"""

from __future__ import annotations

import hashlib
import re
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
# File copiers: what lands at the destination is another file's content, so it is unknown here.
_COPIERS = frozenset({"cp", "mv", "ln", "install"})
_COPIER_MENTION = re.compile(r"\b(?:cp|mv|ln|install)\b")


def _copier_targets(operands: list[str]) -> list[str]:
    """Paths a ``cp``/``mv``/``ln``/``install`` may create: ``DIR/<basename>`` of each source with
    ``-t DIR``/``--target-directory=DIR``, else the last operand and ``<last>/<basename>`` of each
    other operand (``cp G sub/``). An option value read as an operand only adds paths."""
    paths: list[str] = []
    directory: Optional[str] = None
    words = iter(operands)
    for word in words:
        if word in ("-t", "--target-directory"):
            directory = next(words, None)
        elif word.startswith("--target-directory="):
            directory = word.split("=", 1)[1]
        elif not word.startswith("-"):
            paths.append(word)
    if directory is None:
        if not paths:
            return []
        directory = paths.pop()
        targets = [directory]
    else:
        targets = []
    base = directory.rstrip("/") or directory
    return targets + [f"{base}/{Path(source).name}" for source in paths if Path(source).name]


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


def _command_word(view: list[str], index: int) -> Optional[int]:
    """Index of the command word at or after *index*, skipping redirections there; ``None`` when
    only redirections follow. ``(echo P) > F`` leaves the view ``['>', 'F']`` and ``exec > F``
    (peeled to the ``>``) has no command at all: their writer is unknown (``exec`` sends all
    later output of the shell to F)."""
    while index < len(view):
        match = _REDIRECTION.match(view[index])
        if match is None:
            return index
        index += 1 if match.group(3) else 2
    return None


def _writes(view: list[str], index: Optional[int]) -> tuple[list[str], list[str], dict]:
    """``(stdout targets, other written targets, inputs)`` of the command at *index* (``None``:
    a bare redirection). Every redirection of the view counts, before the command word too
    (``> F echo``, ``exec 3>F``); only words after the command are its operands."""
    stdout: list[str] = []
    other: list[str] = []
    inputs = {"heredoc": False, "herestrings": [], "stdin_file": False, "operands": []}
    words = iter(enumerate(view))
    for position, word in words:
        match = _REDIRECTION.match(word)
        if match is None:
            if index is not None and position > index:
                inputs["operands"].append(word)
            continue
        fd, operator, target = match.groups()
        target = target or next(words, (None, ""))[1]
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
    index = _command_word(view, index)
    name = _executable_name(view[index]) if index is not None else ""
    stdout, other, inputs = _writes(view, index)
    targets = list(stdout)
    if name == "tee":
        targets += [word for word in inputs["operands"] if not word.startswith("-")]
    if name in _COPIERS:
        other += _copier_targets(inputs["operands"])
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
    if ">" not in command and "tee" not in command and not _COPIER_MENTION.search(command):
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
