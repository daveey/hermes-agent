#!/usr/bin/env python3
"""Fail when production code opens a SQLite database or sidecar with a plain file call.

POSIX cancels every advisory lock a process holds on a file when that process closes ANY
descriptor for it. SQLite keeps its WAL locks (the writer lock included) as POSIX locks on
``<db>-shm``, so ``open(f"{db}-shm").read()`` inside a process that also has the database open
releases a write transaction's lock mid-flight, and a second process can then write the same WAL.
That corrupted hermes-hub's state.db on 2026-11-11 (duplicate rows, index/table mismatches,
freelist damage): the session-list cache read the WAL-index header that way on every
``GET /api/sessions``. Raw reads of a live database file or its sidecars go through
``hermes_cli.sqlite_safe_read`` (``pread_lock_safe``, ``read_header_lock_safe``,
``sha256_lock_safe``: a cached descriptor that is never closed; ``copy_file_out_of_process`` for
copies).

Flags, in Python files of the production trees (tests, evals and scripts excluded), a call that
opens, reads, writes or copies a file

  open(...), FileIO(...), copy/copy2/copyfile/copyfileobj/move(...) called bare, and
  <x>.open(...), <x>.FileIO(...), <x>.read_bytes(), <x>.read_text(), <x>.write_bytes(...),
  <x>.write_text(...), <x>.copy/copy2/copyfile/copyfileobj/move(...) on ANY receiver
  (``os``, ``io``, ``shutil`` under any alias, a ``Path``; the method name alone decides)

when it names a sidecar: ANY argument (or the receiver, unless that is ``os``/``io``/``shutil``) contains a
``str``/``bytes`` literal ending in ``.db``, ``-shm``, ``-wal`` or ``-journal`` (f-string fragments count, so do
``name + "-wal"`` and ``"%s-wal" % db``), or a name assigned from such an expression. A literal
that is only tested (``name.endswith("-wal")``, ``x == "-shm"``) does not count.

Read/write text/bytes methods inspect only the receiver, not content or encoding arguments.
Names are tracked without flow, and err toward flagging (rename the variable or mark the line):

  variables      per function: assignment, ``for``/comprehension target, ``with ... as``,
                 a parameter default. Module- and class-level names and those of an enclosing
                 function are visible inside; a parameter does not hide them. A name that holds
                 a sidecar path anywhere in a function is one throughout it, and so is a value
                 derived from it.
  attributes     for the whole file, by attribute name: ``self.shm = db + "-shm"`` in one
                 method, or ``SUFFIX = "-wal"`` in a class body, marks every ``<x>.shm`` /
                 ``<x>.SUFFIX``.

A function of the same file that passes a parameter to such a call (``def _head(path): open(path)``),
directly or through another such function, is a file call too: ``_head(db + "-shm", 0, 48)`` is flagged
at the call site (hermes-hub 2026-11-20: exactly that, in the annotation holder's watch signature, cancelled the
Desktop backend's locks every 2 s and two writers corrupted state.db's indexes).

Not caught: a sidecar path built in another file, by a helper defined elsewhere, or passed in
as a parameter of a function that is called from another file; one stored under a subscript (``d["wal"] = ...; open(d["wal"])``); an aliased
import (``from os import open as os_open``); a database path with no identifying literal;
``open`` under another name (``op = open``); a global one function writes
and another opens; a copy made by a child process; and descriptors opened from C extensions. The runtime side of this rule is ``hermes_state``'s WAL
lock guard; a review still has to catch those.

``hermes_cli/sqlite_safe_read.py`` is not scanned: the lock-safe readers are the one place that
holds a raw descriptor on a sidecar. Calls into it from other files need no exemption, since
``pread_lock_safe(...)`` and friends are not file calls.

Opt out of one line with ``sqlite-sidecar-open: ok — <why>`` on that line or the line directly
above it; a marker without a reason after the dash does not count. Legitimate reasons: this
process provably holds no connection to that database at that point (an offline recovery tool
after it closed its handles), or the file is a detached copy no connection ever opens.

``_BASELINE`` maps a file to a tolerated count of unmarked hits. It is empty; keep it that way.

Run: python scripts/check_sqlite_sidecar_opens.py [--all] [--print-baseline] [--strict-baseline] [paths...]
Exit 1 on any violation, 0 when clean.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKER = "sqlite-sidecar-open: ok"
# The lock-safe readers themselves: the one place a raw descriptor on a sidecar is the point.
HELPER_FILES = {"hermes_cli/sqlite_safe_read.py"}
SCAN_TREES = ("agent", "cron", "gateway", "hermes_cli", "plugins", "tools", "tui_gateway", "acp_adapter", "pm")
SCAN_ROOT_FILES = (":(top,glob)*.py",)
SKIP_PARTS = {"tests", "__pycache__", "evals", "node_modules"}
_SUFFIXES = (".db", "-shm", "-wal", "-journal")
_BYTE_SUFFIXES = tuple(suffix.encode("ascii") for suffix in _SUFFIXES)
# Cheap text prefilter: only files mentioning a sidecar suffix are parsed.
_NEEDLES = _SUFFIXES
_TEST_METHODS = {"endswith", "startswith", "removesuffix", "removeprefix", "count", "find", "index"}
_SHUTIL_FUNCS = {"copy", "copy2", "copyfile", "copyfileobj", "move"}
_OPEN_NAMES = {"open", "FileIO", *_SHUTIL_FUNCS}  # bare names: builtins, ``from shutil import copyfile``
_FILE_METHODS = {"open", "FileIO", "read_bytes", "read_text", "write_bytes", "write_text"}


def _names_sidecar(node: ast.AST, tainted: set[str] | frozenset[str]) -> bool:
    """Whether the expression ``node`` mentions a ``-shm``/``-wal`` literal or a tainted variable."""
    stack: list[ast.AST] = [node]
    while stack:
        sub = stack.pop()
        # A suffix that is only tested (``name.endswith("-wal")``, ``x == "-shm"``) builds no path.
        if isinstance(sub, ast.Compare) or (isinstance(sub, ast.Call) and getattr(sub.func, "attr", None) in _TEST_METHODS):
            continue
        stack.extend(ast.iter_child_nodes(sub))
        if isinstance(sub, ast.Constant) and (
                (isinstance(sub.value, str) and sub.value.endswith(_SUFFIXES))
                or (isinstance(sub.value, bytes) and sub.value.endswith(_BYTE_SUFFIXES))):
            return True
        if isinstance(sub, ast.Name) and sub.id in tainted:
            return True
        if isinstance(sub, ast.Attribute) and "." + sub.attr in tainted:
            return True
    return False


def _target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):  # self.shm = ...: tracked by attribute name, as ".shm"
        return ["." + target.attr]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for elt in target.elts for name in _target_names(elt)]
    return []


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes of ``scope`` itself, not those of functions nested in it."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        out.append(node)
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))
    return out


def _tainted_names(nodes: list[ast.AST], inherited: frozenset[str]) -> frozenset[str]:
    """``inherited`` plus the names ``nodes`` assign from an expression naming a sidecar (to a
    fixed point, so ``wal = db + "-wal"; src = wal`` marks both). ``self.x`` is kept as ``.x``."""
    assigns: list[tuple[list[str], ast.AST]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            names = [name for target in node.targets for name in _target_names(target)]
            assigns.append((names, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr, ast.AugAssign)) and node.value is not None:
            assigns.append((_target_names(node.target), node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):  # for p in (db + "-wal", ...)
            assigns.append((_target_names(node.target), node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:  # with x(db + "-shm") as p
            assigns.append((_target_names(node.optional_vars), node.context_expr))
        elif isinstance(node, ast.arguments):  # def f(p=db + "-wal")
            positional = [*node.posonlyargs, *node.args]
            pairs = [*zip(positional[len(positional) - len(node.defaults):], node.defaults),
                     *zip(node.kwonlyargs, node.kw_defaults)]
            assigns.extend(([arg.arg], default) for arg, default in pairs if default is not None)
        elif isinstance(node, ast.ClassDef):  # class C: SUFFIX = "-wal" -> self.SUFFIX / C.SUFFIX
            for stmt in node.body:
                if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                    names = ["." + name for target in targets for name in _target_names(target)
                             if not name.startswith(".")]
                    assigns.append((names, stmt.value))
    tainted = set(inherited)
    changed = True
    while changed:
        changed = False
        for names, value in assigns:
            if names and not set(names) <= tainted and _names_sidecar(value, tainted):
                tainted.update(names)
                changed = True
    return frozenset(tainted)


Openers = dict[str, tuple[frozenset[int], frozenset[str]]]  # function name -> (path positions, path keywords)


def _opener_call_paths(node: ast.Call, openers: Openers) -> list[ast.AST]:
    """Path arguments of a call to a same-file function that opens one of its parameters."""
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
        name = func.attr
    else:
        return []
    if name not in openers:
        return []
    positions, keywords = openers[name]
    return [*(arg for i, arg in enumerate(node.args) if i in positions),
            *(kw.value for kw in node.keywords if kw.arg in keywords)]


def _openers(tree: ast.AST, src_lines: list[str] | None = None) -> Openers:
    """Functions of this file that pass a parameter to a file call, directly or through another opener
    (``def _head(path): open(path, "rb")``): calling one on a sidecar is a plain file call on it, so
    the call site is flagged. Matched by bare name or ``self.``/``cls.`` method name, to a fixed point. A file
    call carrying the opt-out marker does not make its function an opener."""
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    openers: Openers = {}
    changed = True
    while changed:
        changed = False
        for fn in functions:
            params = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
            names = {arg.arg for arg in params}
            opened: set[str] = set()
            for node in _scope_nodes(fn):
                if isinstance(node, ast.Call) and not (src_lines and _marked(src_lines, node.lineno)):
                    for path in [*_file_call_paths(node), *_opener_call_paths(node, openers)]:
                        opened.update(sub.id for sub in ast.walk(path) if isinstance(sub, ast.Name) and sub.id in names)
            if not opened:
                continue
            positional = [*fn.args.posonlyargs, *fn.args.args]
            offset = 1 if positional and positional[0].arg in {"self", "cls"} else 0
            entry = (frozenset(i - offset for i, arg in enumerate(positional) if arg.arg in opened and i >= offset),
                     frozenset(opened))
            if openers.get(fn.name) != entry:
                openers[fn.name] = entry
                changed = True
    return openers


def _file_call_paths(node: ast.Call) -> list[ast.AST]:
    """The path-bearing expressions of ``node`` when it is a file open/read/write/copy call."""
    func = node.func
    args: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
    if isinstance(func, ast.Name):
        return args if func.id in _OPEN_NAMES else []
    if not isinstance(func, ast.Attribute):
        return []
    owner = func.value.id if isinstance(func.value, ast.Name) else None
    if func.attr in {"read_bytes", "read_text", "write_bytes", "write_text"}:
        return [func.value]  # payload/encoding are not paths
    if func.attr in _FILE_METHODS or func.attr in _SHUTIL_FUNCS:
        # os.open(path, ...), shutil.copyfile(src, dst): only the arguments are paths. For
        # Path(path).open(), path.read_bytes(), path.copy(dst) the receiver is one too.
        return args if owner in {"os", "io", "shutil"} else [func.value, *args]
    return []


def sidecar_open_lines(text: str) -> list[int]:
    """Line numbers of plain file calls on a ``-shm``/``-wal`` path in ``text`` (a Python module)."""
    if not any(needle in text for needle in _NEEDLES):
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    # Attribute names (``self.shm = db + "-shm"``) are shared by the whole file: the method that
    # sets one is rarely the method that opens it.
    attrs: frozenset[str] = frozenset()
    while True:
        found_attrs = set(attrs)
        pending: list[tuple[ast.AST, frozenset[str]]] = [(tree, attrs)]
        while pending:
            scope, inherited = pending.pop()
            nodes = _scope_nodes(scope)
            tainted = _tainted_names(nodes, inherited)
            found_attrs.update(n for n in tainted if n.startswith("."))
            pending.extend((node, tainted) for node in nodes if isinstance(node, _SCOPES))
        if found_attrs == attrs:
            break
        attrs = frozenset(found_attrs)
    openers = _openers(tree, text.splitlines())
    lines: set[int] = set()
    scopes: list[tuple[ast.AST, frozenset[str]]] = [(tree, attrs)]
    while scopes:
        scope, inherited = scopes.pop()
        nodes = _scope_nodes(scope)
        tainted = _tainted_names(nodes, inherited)
        for node in nodes:
            if isinstance(node, _SCOPES):
                scopes.append((node, tainted))  # a nested function sees the names of the one around it
            elif isinstance(node, ast.Call) and any(
                    _names_sidecar(path, tainted)
                    for path in [*_file_call_paths(node), *_opener_call_paths(node, openers)]):
                lines.add(node.lineno)
    return sorted(lines)


# The marker counts only with a reason: ``sqlite-sidecar-open: ok — <why>`` (an em dash or hyphens).
_MARKED = re.compile(re.escape(MARKER) + r"\s*(?:—|-+)\s*\S")


def _marked(src_lines: list[str], lineno: int) -> bool:
    here = src_lines[lineno - 1] if lineno - 1 < len(src_lines) else ""
    above = src_lines[lineno - 2] if lineno >= 2 else ""
    return bool(_MARKED.search(here) or _MARKED.search(above))


def _tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", *SCAN_TREES, *SCAN_ROOT_FILES],
                             capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Fail closed: scanning nothing would report "no sidecar opens".
        raise RuntimeError(f"cannot list the tracked files under {root}: {exc}") from exc
    return [root / rel for rel in out.split("\0") if rel.endswith(".py")]


def _in_scope(rel: str) -> bool:
    parts = Path(rel).parts
    return not (SKIP_PARTS & set(parts)) and not Path(rel).name.startswith("test_") and rel not in HELPER_FILES


def scan(paths=None, root: Path | None = None) -> dict[str, list[tuple[int, str]]]:
    """Repo-relative path -> [(lineno, line)] of unmarked sidecar opens."""
    root = (root or ROOT).resolve()
    hits: dict[str, list[tuple[int, str]]] = {}
    for path in (list(paths) if paths else _tracked_files(root)):
        path = Path(path).resolve()
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        if path.suffix != ".py" or not _in_scope(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        src = text.splitlines()
        found = [(n, src[n - 1]) for n in sidecar_open_lines(text) if not _marked(src, n)]
        if found:
            hits[rel] = found
    return hits


# Burn-down list of files with unmarked hits. Empty: route the read through sqlite_safe_read or
# mark the line with its reason instead of adding an entry.
_BASELINE: dict[str, int] = {
}


def _format_baseline(hits: dict[str, list]) -> str:
    body = "".join(f'    "{rel}": {len(found)},\n' for rel, found in sorted(hits.items()))
    return "_BASELINE: dict[str, int] = {\n" + body + "}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("paths", nargs="*", help="files to check (default: the production trees)")
    ap.add_argument("--all", action="store_true", help="ignore _BASELINE; report every hit")
    ap.add_argument("--print-baseline", action="store_true", help="print a _BASELINE for the current tree")
    ap.add_argument("--strict-baseline", action="store_true", help="also fail when _BASELINE overstates a file")
    args = ap.parse_args(argv)

    try:
        hits = scan(args.paths or None)
    except RuntimeError as exc:
        print(f"check_sqlite_sidecar_opens: {exc}", file=sys.stderr)
        return 2
    if args.print_baseline:
        print(_format_baseline(hits))
        return 0
    baseline = {} if (args.all or args.paths) else _BASELINE
    problems: list[str] = []
    advisories: list[str] = []
    for rel in sorted(hits):
        allowed = baseline.get(rel)
        if allowed is not None and len(hits[rel]) <= allowed:
            if len(hits[rel]) < allowed:
                advisories.append(f"{rel}: {len(hits[rel])} sidecar file call(s) left, _BASELINE says {allowed}")
            continue
        problems.extend(f"{rel}:{n}: {line.strip()[:160]}" for n, line in hits[rel])
    advisories.extend(f"{rel}: listed in _BASELINE but clean (or gone)" for rel in sorted(baseline) if rel not in hits)
    if advisories:
        print("advisory — _BASELINE in scripts/check_sqlite_sidecar_opens.py is stale; regenerate it with "
              "--print-baseline (fewer hits than listed is progress, not a failure):")
        print("\n".join("  " + a for a in advisories))
    if args.strict_baseline:
        problems.extend(advisories)
    if not problems:
        print("no plain file calls on SQLite .db/-shm/-wal/-journal paths outside sqlite_safe_read")
        return 0
    print("\n".join(problems))
    print(f"\n{len(problems)} plain file call(s) on a SQLite database or sidecar. Closing any descriptor on a "
          "sidecar cancels this process's SQLite locks on it (a write transaction's writer lock included), "
          "so another process can write the same WAL. Read it with hermes_cli.sqlite_safe_read "
          "(pread_lock_safe / read_header_lock_safe / sha256_lock_safe, copy_file_out_of_process), or mark a "
          f"call made while this process holds no connection to that database with `{MARKER} — <why>` "
          "(same line or the line above).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
