"""Shell command lines must quote the interpreter path.

A fixture or launcher that drops ``sys.executable`` into a shell line as a bare
command word, e.g. ``f"exec {sys.executable} -c ..."``, breaks as soon as the
interpreter path contains a space. On macOS the signed production interpreter
lives under ``~/Applications/Hermes Python.app/``, so ``sh`` runs ``.../Hermes``
and exits 127. Tests that only need the helper's PID then pass without the helper
ever starting, which hides the failure here while consumers that need a live
helper fail.

Quote it (``shlex.quote(sys.executable)`` or a quoted literal) or pass an argv
list. Shebang lines (``#!{sys.executable}``) are a different limit: the kernel
splits them at the first space and no quoting helps, so they are not checked.
"""

from __future__ import annotations
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import ast
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "website",
    "apps",
    "build",
    "dist",
    ".worktrees",
}
_WORD_START = (" ", "\t", "\n", ";", "&", "|", "(", "`")
# Lines that name the interpreter in text no shell runs: (repo path, stripped source line) -> reason.
_NOT_SHELL = {
    (
        "tests/hermes_cli/test_gateway_runtime_service.py",
        "command = f'{sys.executable} -m hermes_cli.main gateway run'",
    ): "fake systemd ExecStart argv; the fixture splits it itself",
}
_OPAQUE = object()


def _is_interpreter(node: object) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr in {"executable", "_base_executable"}
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _added(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _added(node.left) + _added(node.right)
    return [node]


def _parts(node: ast.AST) -> list | None:
    """Literal text as str, a bare interpreter as its node, anything else opaque."""
    if isinstance(node, ast.JoinedStr):
        out: list = []
        for value in node.values:
            if isinstance(value, ast.Constant) or (
                isinstance(value, ast.FormattedValue)
                and value.conversion == -1
                and value.format_spec is None
                and _is_interpreter(value.value)
            ):
                out.append(value.value)
            else:
                out.append(_OPAQUE)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [
            p.value
            if isinstance(p, ast.Constant) and isinstance(p.value, str)
            else (p if _is_interpreter(p) else _OPAQUE)
            for p in _added(node)
        ]
    return None


def _bare_word(parts: list) -> bool:
    for i, part in enumerate(parts):
        if not _is_interpreter(part):
            continue
        before = "" if i == 0 else parts[i - 1]
        after = parts[i + 1] if i + 1 < len(parts) else ""
        if not isinstance(before, str) or not isinstance(after, str):
            continue
        if before.endswith("#!"):
            continue  # shebang: see the module docstring
        if before and not before.endswith(_WORD_START):
            continue  # quoted ('...', "...") or part of other text (key=value)
        if after[:1].isspace():
            return True
    return False


def offending_lines(source: str, filename: str = "<string>") -> list[int]:
    lines = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, (ast.JoinedStr, ast.BinOp)):
            continue
        parts = _parts(node)
        if parts and _bare_word(parts):
            lines.add(node.lineno)
    return sorted(lines)


def _python_files():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(root, name)


@pytest.mark.parametrize(
    "snippet",
    [
        'f"exec {sys.executable} -c pass"',
        'f"{sys.executable} -m pip install x"',
        'sys.executable + " -c pass"',
        '"exec " + sys.executable + " -c pass"',
        'f"  {sys.executable} {script} --repo ."',
        'f"#!/bin/sh\\nexec {sys.executable} x"',
    ],
)
def test_detector_flags_a_bare_interpreter_word(snippet):
    assert offending_lines(snippet) == [1]


@pytest.mark.parametrize(
    "snippet",
    [
        'f"exec {shlex.quote(sys.executable)} -c pass"',
        "f'exec \"{sys.executable}\" -c pass'",
        "f\"exec '{sys.executable}' -c pass\"",
        'f"#!{sys.executable}\\nimport sys"',
        '"#!" + sys.executable + "\\nimport sys"',
        'f"ExecStart={sys.executable} -m x"',
        'f"{sys.executable!r} -c pass"',
        '[sys.executable, "-c", "pass"]',
        'f"{sys.executable}"',
        'sys.executable + "-missing"',
        'f"python is {sys.executable}: missing x"',
    ],
)
def test_detector_allows_quoted_or_non_shell_text(snippet):
    assert offending_lines(snippet) == []


def test_shell_lines_quote_the_interpreter():
    offenders = []
    for path in _python_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "executable" not in text:
            continue
        try:
            found = offending_lines(text, rel)
        except SyntaxError:
            continue
        source = text.splitlines()
        for lineno in found:
            line = source[lineno - 1].strip()
            if (rel, line) not in _NOT_SHELL:
                offenders.append(f"{rel}:{lineno}: {line[:120]}")
    assert not offenders, (
        "quote sys.executable in shell lines (shlex.quote(sys.executable)) or pass an argv list:\n"
        + "\n".join(offenders)
    )


def test_not_shell_entries_still_match_a_line():
    for rel, line in _NOT_SHELL:
        source = [
            row.strip() for row in (REPO / rel).read_text(encoding="utf-8").splitlines()
        ]
        assert line in source, f"stale _NOT_SHELL entry: {rel}: {line}"
