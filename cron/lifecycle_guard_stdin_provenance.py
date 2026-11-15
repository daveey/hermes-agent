"""Small reaching-definition proof for nested-shell stdin, conservative otherwise.

Segment views intentionally discard shell control flow. Never infer a reaching
assignment from their union: only a flat, unconditional sequence is a proof.
"""

from __future__ import annotations

import re
from collections.abc import Iterator


def source_contexts(command: str, views: list[list[str]]) -> Iterator:
    """Variable and descriptor snapshots immediately before each command view."""
    from cron.lifecycle_guard_nested_shells import (
        _StdinSources, _assigned_variables, _redirections, _split_redirections,
    )

    fds: dict[str, list[tuple[str, str]]] = {}
    flat = _flat_sequence(command, views)
    for view, variables in zip(views, variable_contexts(command, views, flat=flat)):
        yield _StdinSources(dict(fds), variables)
        if flat and _split_redirections(view)[0] == ["exec"]:
            for fd, operator, target in _redirections(view):
                if fd.isdigit():
                    fds[fd] = descriptor_definition(operator, target, fds, variables)
        elif not (flat and _assigned_variables(iter([view]))):
            # A command can call a function or builtin which replaces descriptors.
            fds = {}


def descriptor_definition(operator, target, fds, variables) -> list[tuple[str, str]]:
    """Freeze a descriptor at its redirection, including duplication order."""
    from cron.lifecycle_guard_nested_shells import _expanded_targets

    if operator == "<&":
        return [("<", "/dev/null")] if target == "-" else list(fds.get(target, [("?", "")]))
    if operator in ("<", "<>"):
        paths = _expanded_targets(target, variables)
        return [(operator, path) for path in paths] if paths else [("?", target)]
    return [(operator, target)]


def variable_contexts(
    command: str, views: list[list[str]], *, flat: bool | None = None,
) -> Iterator[dict[str, list[str]]]:
    """Snapshot variables before each view; unsupported scope or mutation is unknown.

    This is deliberately not a shell evaluator. Only standalone assignments in
    a flat list can replace inherited values, and expansions are frozen when
    assigned rather than re-expanded against a later variable definition.
    """
    from cron.lifecycle_guard_nested_shells import (
        _VARIABLE_RE, _assigned_variables, _expanded_targets,
    )

    unknown = {match.group(1) or match.group(2): ["$"]
               for match in _VARIABLE_RE.finditer(command)}
    if flat is None:
        flat = _flat_sequence(command, views)
    state: dict[str, list[str]] = {} if flat else unknown
    for view in views:
        yield dict(state)
        if not flat:
            continue
        assignments = _assigned_variables(iter([view]))
        if view and (view[0] in {"declare", "typeset", "local", "readonly"}
                     or any(word.startswith("-") for word in view)):
            assignments = {}
        if not assignments:
            # read, unset, eval, source, function calls, namerefs, arithmetic,
            # etc. can mutate values, including persistent reference semantics.
            state = dict(unknown)
            flat = False
            continue
        before = dict(state)
        words = view[1:] if view[0] == "export" else view
        for word in words:
            name, _, value = word.partition("=")
            expansion = before if view[0] == "export" else state
            state[name] = _expanded_targets(value, expansion) or ["$"]


def _flat_sequence(command: str, views: list[list[str]]) -> bool:
    """Accept only simple sequential commands, never flattened compound views."""
    from cron.lifecycle_guard import _iter_command_segment_views
    from cron.lifecycle_guard_nested_shells import _VARIABLE_RE, _assigned_variables

    syntax = _VARIABLE_RE.sub("VARIABLE", command).replace("<&", "<")
    if re.search(r"[(){}|&`\\]", syntax) or any(
            "$" in quoted for quoted in re.findall(r"'[^']*'", command)):
        return False
    chunks = [chunk.strip() for chunk in re.split(r"[;\n]", command) if chunk.strip()]
    parts = [list(_iter_command_segment_views(chunk)) for chunk in chunks]
    if any(len(part) != 1 for part in parts) or [part[0] for part in parts] != views:
        return False
    # Quoting any part of NAME= makes it a command word, not an assignment.
    # Only prove the deliberately small unquoted assignment grammar here.
    assignment = r"(?:export\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=[^\s'\";]*\s*)+"
    if any(_assigned_variables(iter([view])) and not re.fullmatch(assignment, chunk)
           for chunk, view in zip(chunks, views)):
        return False
    reserved = {"if", "then", "else", "elif", "fi", "for", "while", "until", "do",
                "done", "case", "esac", "select", "function", "!", "time", "coproc"}
    return all(view and view[0] not in reserved for view in views)
