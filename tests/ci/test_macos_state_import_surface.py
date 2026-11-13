"""The macOS state lane starts for every first-party module hermes_state* imports at load time.

A break in such a module fails ``import hermes_state`` on the mini, yet a change to it
alone did not select the PR lane (#1538 review notes 4 and 5). The
surface is exactly the import-time imports, read from the source: function-local
(lazy) imports and ``if TYPE_CHECKING:`` blocks are left out, and so are the
imports of those modules (the 4-hourly schedule tests main for the rest).
"""
import ast
import subprocess
from pathlib import Path

import pytest

from scripts.ci.classify_changes import HERMES_STATE_IMPORTS, classify

ROOT = Path(__file__).resolve().parents[2]


def _module_files(root, dotted):
    """Repo files Python executes for ``import <dotted>``: each package __init__, then the module."""
    parts, out = dotted.split('.'), []
    for i in range(1, len(parts) + 1):
        base = root.joinpath(*parts[:i])
        if (base / '__init__.py').is_file():
            out.append(base / '__init__.py')
        elif i == len(parts) and base.with_suffix('.py').is_file():
            out.append(base.with_suffix('.py'))
        elif not base.is_dir():
            return out if i > 1 else []  # an attribute of the module above, or not first-party
    return out


def _is_type_checking(test):
    """Exactly ``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``; ``not TYPE_CHECKING`` etc. run at load time."""
    return ((isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING')
            or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING'))


def _load_time_import_nodes(tree):
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            stack.extend(node.orelse)
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            continue
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                stack.append(child)


def load_time_imports(root, path):
    """First-party files executed when ``path`` (a top-level module) is imported."""
    tree = ast.parse(path.read_text(encoding='utf-8'), str(path))
    package = list(path.relative_to(root).parent.parts)
    files = set()
    for node in _load_time_import_nodes(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            module = node.module
            if node.level:
                module = '.'.join(package[:len(package) - node.level + 1] + ([module] if module else []))
            names = [module] + [f'{module}.{alias.name}' for alias in node.names if alias.name != '*']
        for name in filter(None, names):
            files.update(_module_files(root, name))
    return {f.relative_to(root).as_posix() for f in files}


def state_import_surface(root):
    tracked = subprocess.run(['git', 'ls-files', '-z', '--', 'hermes_state*.py', 'hermes_state*/**.py'],
                             cwd=root, capture_output=True, text=True, check=True, timeout=60)
    seeds = [root / f for f in tracked.stdout.split('\0') if f]
    assert seeds, 'no hermes_state* modules found'
    found = set().union(*(load_time_imports(root, seed) for seed in seeds))
    return sorted(f for f in found if not f.startswith('hermes_state'))


def test_scanner_reads_only_load_time_first_party_imports(tmp_path):
    for rel in ('pkg/__init__.py', 'pkg/sub.py', 'pkg/lazy.py', 'pkg/typed.py', 'top.py', 'guarded.py',
                'fallback.py', 'other.py', 'negated.py', 'either.py', 'matched.py'):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text('')
    seed = tmp_path / 'hermes_state_x.py'
    seed.write_text(
        'import os\nfrom typing import TYPE_CHECKING\nimport top\nfrom pkg import sub, NAME\n'
        'if TYPE_CHECKING:\n    from pkg import typed\nelse:\n    import other\n'
        'try:\n    import guarded\nexcept ImportError:\n    import fallback\n'
        'def f():\n    from pkg import lazy\n'
        'class C:\n    import top as again\n'
        'if not TYPE_CHECKING:\n    import negated\n'
        'if TYPE_CHECKING or os.name:\n    import either\n'
        'match os.name:\n    case _:\n        import matched\n')
    assert load_time_imports(tmp_path, seed) == {
        'top.py', 'pkg/__init__.py', 'pkg/sub.py', 'other.py', 'guarded.py', 'fallback.py',
        'negated.py', 'either.py', 'matched.py'}


def test_surface_lists_exactly_hermes_states_load_time_imports():
    # New import -> add it; dropped import -> remove it. Keeps the lane narrow and complete.
    assert sorted(HERMES_STATE_IMPORTS) == state_import_surface(ROOT)


@pytest.mark.parametrize('path', state_import_surface(ROOT))
def test_each_import_starts_lane_on_pr(path):
    assert classify([path])['hermes_state'], path
