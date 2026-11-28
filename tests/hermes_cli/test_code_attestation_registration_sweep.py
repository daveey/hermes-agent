"""The pre-registration module sweep keeps pathlib's answer without building a Path per module.

Component registration checks every loaded module once per reviewed plugin root, on the
serve boot's critical path (run68: ~40% of plugin discovery on a production-shaped copy).
"""
import collections
import os
import sys
import threading
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation as ca

posix_only = pytest.mark.skipif(os.name != 'posix', reason='POSIX realpath fast path')


def _ledger(modules):
    ledger = object.__new__(ca.Attestation)
    ledger._lock = threading.RLock()
    ledger._components = {}
    ledger._generation = 0
    ledger._unknown = set()
    ledger._module_snapshot = lambda: tuple(modules.items())
    return ledger


def _tree(tmp_path):
    root = tmp_path / 'plugins' / 'reviewed'
    (root / 'pkg').mkdir(parents=True)
    (root / 'pkg' / 'a.py').write_text('')
    other = tmp_path / 'plugins' / 'reviewed-other'
    other.mkdir()
    (other / 'b.py').write_text('')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'c.py').write_text('')
    (tmp_path / 'link-in').symlink_to(root, target_is_directory=True)
    (tmp_path / 'link-out').symlink_to(outside, target_is_directory=True)
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    (root / 'loop').symlink_to(root / 'loop')
    return root


def _candidates(tmp_path, root):
    t = str(tmp_path)
    return [
        str(root / 'pkg' / 'a.py'), str(root), str(root) + '/', str(root / 'pkg') + '//a.py',
        str(root / 'missing' / 'x.py'), t + '/plugins/reviewed-other/b.py', t + '/plugins/reviewed-other',
        t + '/link-in/pkg/a.py', t + '/link-out/c.py', str(root / 'escape' / 'c.py'),
        str(root / 'pkg' / '..' / '..' / 'reviewed-other' / 'b.py'), str(root / 'pkg' / '.' / 'a.py'),
        str(root / 'loop' / 'x.py'), '/' + str(root), 'relative/x.py', os.path.relpath(root / 'pkg' / 'a.py'),
        '.', '/',
    ]


@posix_only
def test_fast_path_matches_pathlib_for_real_filesystem_shapes(tmp_path, monkeypatch):
    root = _tree(tmp_path).resolve()
    (root / 'pkg' / 'linked.py').symlink_to(tmp_path / 'outside' / 'c.py')
    (tmp_path / 'outside' / 'back.py').symlink_to(root / 'pkg' / 'a.py')
    (tmp_path / 'outside' / 'dangling.py').symlink_to(tmp_path / 'nowhere.py')
    (tmp_path / 'outside' / 'rel.py').symlink_to('../plugins/reviewed/pkg/a.py')
    monkeypatch.chdir(tmp_path / 'plugins')
    extra = ['reviewed/pkg/a.py', 'reviewed', 'reviewed-other/b.py', 'a.py', str(root / 'pkg' / 'linked.py'),
             str(tmp_path / 'outside' / 'back.py'), str(tmp_path / 'outside' / 'dangling.py'),
             str(tmp_path / 'outside' / 'rel.py'), str(tmp_path / 'link-out' / 'back.py'), '//' + str(root / 'pkg' / 'a.py'),
             str(root / 'loop'), str(root / 'pkg' / 'a.py' / 'x.py'), str(root / 'pkg') + '/..', str(root / 'pkg') + '/.']
    names = _candidates(tmp_path, root) + extra
    # Every resolved root an answer could hinge on: each name's resolved path and all its ancestors.
    roots = {r for n in names for p in [Path(n).resolve()] for r in (p, *p.parents)} | {(tmp_path / 'outside').resolve()}
    assert all(r == r.resolve() for r in roots)
    for candidate_root in sorted(roots):
        shared = {}  # one memo across the whole sweep, as registration uses it
        for filename in names + names:
            expected = Path(filename).resolve().is_relative_to(candidate_root)
            assert ca._resolves_under(filename, candidate_root) is expected, (filename, candidate_root)
            assert ca._resolves_under(filename, candidate_root, shared) is expected, (filename, candidate_root)


@posix_only
def test_fast_path_observes_symlink_retarget_between_calls(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(first, target_is_directory=True)
    filename = str(link / 'm.py')
    assert ca._resolves_under(filename, first.resolve())
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert not ca._resolves_under(filename, first.resolve())
    assert ca._resolves_under(filename, second.resolve())


@pytest.mark.parametrize('filename', [b'/x/y.py', Path('/x/y.py'), PurePosixPath('/x/y.py')])
def test_non_string_filenames_keep_the_pathlib_answer_and_errors(filename):
    root = Path('/x')
    try:
        expected = Path(filename).resolve().is_relative_to(root)
    except TypeError:
        with pytest.raises(TypeError):
            ca._resolves_under(filename, root)
    else:
        assert ca._resolves_under(filename, root) is expected


def test_embedded_nul_error_is_unchanged():
    with pytest.raises(ValueError):
        Path('/x/\0.py').resolve()
    for parents in (None, {}, {'/x': '/x'}):
        with pytest.raises(ValueError):
            ca._resolves_under('/x/\0.py', Path('/x'), parents)
        with pytest.raises(ValueError):
            ca._resolves_under('/x\0/a.py', Path('/x'), parents)


@posix_only
def test_registration_marks_exactly_the_modules_resolving_under_root(tmp_path):
    root = _tree(tmp_path)
    modules = {
        'inside': SimpleNamespace(__file__=str(root / 'pkg' / 'a.py')),
        'via_link': SimpleNamespace(__file__=str(tmp_path / 'link-in' / 'pkg' / 'a.py')),
        'sibling_prefix': SimpleNamespace(__file__=str(tmp_path / 'plugins' / 'reviewed-other' / 'b.py')),
        'escaping_link': SimpleNamespace(__file__=str(root / 'escape' / 'c.py')),
        'builtin': SimpleNamespace(),
        'empty': SimpleNamespace(__file__=''),
        'none': SimpleNamespace(__file__=None),
    }
    ledger = _ledger(modules)
    ledger.register_component_root('plugin:reviewed', tmp_path / 'link-in', 'plugin', digests={'pkg/a.py': 'x'})
    assert ledger._components['plugin:reviewed'][0] == root.resolve()
    assert ledger._unknown == {'pre-registration-module:inside', 'pre-registration-module:via_link'}


@posix_only
def test_registration_sweep_builds_no_path_per_module(tmp_path):
    """RED on the per-module ``Path(filename).resolve().is_relative_to(root)`` sweep."""
    root = _tree(tmp_path)
    modules = {f'm{i}': SimpleNamespace(__file__=str(tmp_path / 'outside' / f'm{i}.py')) for i in range(50)}
    ledger = _ledger(modules)
    codes = {Path.resolve.__code__: 'resolve', Path.is_relative_to.__code__: 'is_relative_to'}
    calls = {'resolve': 0, 'is_relative_to': 0}

    def observe(frame, event, _arg):
        if event == 'call' and frame.f_code in codes:
            calls[codes[frame.f_code]] += 1

    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        ledger.register_component_root('plugin:reviewed', root, 'plugin', digests={'pkg/a.py': 'x'})
    finally:
        sys.setprofile(previous)
    assert ledger._unknown == set()
    # One resolve for the root itself; none per module, and no ancestor walk per module.
    assert calls == {'resolve': 1, 'is_relative_to': 0}


@posix_only
def test_registration_sweep_walks_each_parent_directory_once(tmp_path, monkeypatch):
    """RED on a per-module full ``realpath``: 50 siblings must not re-walk their parent."""
    root = _tree(tmp_path)
    deep = tmp_path / 'outside' / 'a' / 'b' / 'c'
    deep.mkdir(parents=True)
    modules = {f'm{i}': SimpleNamespace(__file__=str(deep / f'm{i}.py')) for i in range(50)}
    ledger = _ledger(modules)
    seen = []
    real_lstat = os.lstat
    depth = [0]

    def spy(p, *a, **k):
        # Count only the code under test's own calls, not the test harness's I/O guard,
        # which wraps os.lstat and resolves each path again through this same seam.
        if not depth[0]:
            seen.append(os.fspath(p))
        depth[0] += 1
        try:
            return real_lstat(p, *a, **k)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(ca.os, 'lstat', spy)
    ledger.register_component_root('plugin:reviewed', root, 'plugin', digests={'pkg/a.py': 'x'})
    assert ledger._unknown == set()
    per = collections.Counter(seen)
    files = {str(deep / f'm{i}.py') for i in range(50)}
    assert set(per) >= files
    # Each module's last component is lstat()ed once; its parent is walked once per sweep,
    # not once per module (a full per-module realpath walks ``deep`` 50 times).
    assert per[str(deep)] == 1 and all(per[f] == 1 for f in files)
