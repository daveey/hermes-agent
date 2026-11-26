"""Shared owner classification preserves lexical identity without a redundant predicate."""
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation as ca


def test_owner_path_uses_one_converter_without_redundant_containment(tmp_path):
    root = tmp_path / 'core'
    path = root / 'nested' / 'module.py'
    digests = {'nested/module.py': 'source-digest'}
    ledger = SimpleNamespace(_components={'core': (root, 'core', digests)})
    codes = {Path.is_relative_to.__code__: 'predicate', Path.relative_to.__code__: 'converter'}
    calls = {'predicate': 0, 'converter': 0}

    def observe(frame, event, _arg):
        if event == 'call' and frame.f_code in codes:
            calls[codes[frame.f_code]] += 1

    previous = sys.getprofile()
    try:
        sys.setprofile(observe)
        result = ca.Attestation._owner_path(ledger, path)
    finally:
        sys.setprofile(previous)
    assert result == ('core', 'core', path, 'nested/module.py', digests)
    assert result[2] is path and result[4] is digests
    assert calls == {'predicate': 0, 'converter': 1}


@pytest.mark.parametrize('path_type', [Path, PurePosixPath, PureWindowsPath])
@pytest.mark.parametrize(('filename', 'roots', 'owner', 'relative'), [
    ('core/nested/a.py', ['core'], 0, 'nested/a.py'),
    ('core', ['core'], 0, '.'),
    ('core-other/a.py', ['core'], None, None),
    ('outside/a.py', ['core'], None, None),
    ('core/nested/a.py', ['core', 'core/nested'], 0, 'nested/a.py'),
    ('core/nested/a.py', ['core/nested', 'core'], 0, 'a.py'),
    ('core/a.py', ['outside', 'core'], 1, 'a.py'),
    ('core/../outside/a.py', ['core'], 0, '../outside/a.py'),
    ('/core/a.py', ['core'], None, None),
    ('core/a.py', ['/core'], None, None),
    ('core/a.py', [], None, None),
])
def test_owner_path_preserves_real_lexical_semantics(path_type, filename, roots, owner, relative):
    components = {str(i): (path_type(root), 'kind', {}) for i, root in enumerate(roots)}
    ledger = SimpleNamespace(_components=components)
    path = path_type(filename)
    result = ca.Attestation._owner_path(ledger, path)
    if owner is None:
        assert result is None
    else:
        assert result == (str(owner), 'kind', path, relative, components[str(owner)][2])
        assert result[2] is path and result[4] is components[str(owner)][2]


@pytest.mark.parametrize('error_type', [RuntimeError, TypeError])
def test_owner_converter_unexpected_error_propagates(error_type):
    class BrokenPath:
        def relative_to(self, _root):
            raise error_type('converter_failure')

    ledger = SimpleNamespace(_components={'core': (Path('core'), 'core', {})})
    with pytest.raises(error_type, match='^converter_failure$'):
        ca.Attestation._owner_path(ledger, BrokenPath())


def test_owner_converter_value_error_continues_to_next_root():
    roots = [Path('outside'), Path('core')]
    ledger = SimpleNamespace(_components={str(i): (root, 'kind', {}) for i, root in enumerate(roots)})
    result = ca.Attestation._owner_path(ledger, Path('core/file.py'))
    assert result[0] == '1' and result[3] == 'file.py'


@pytest.mark.parametrize('error_type', [ValueError, RuntimeError])
def test_owner_formatting_error_is_not_a_root_miss(error_type):
    class BrokenRelative:
        def as_posix(self):
            raise error_type('formatting_failure')

    class PathSeam:
        def relative_to(self, _root):
            return BrokenRelative()

    ledger = SimpleNamespace(_components={'core': (Path('core'), 'core', {})})
    with pytest.raises(error_type, match='^formatting_failure$'):
        ca.Attestation._owner_path(ledger, PathSeam())


def test_owner_resolution_observes_mutated_symlink_target(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(first, target_is_directory=True)
    ledger = object.__new__(ca.Attestation)
    ledger._components = {'first': (first, 'core', {}), 'second': (second, 'extension', {})}
    filename = str(link / 'file.py')
    assert ledger._owner(filename)[0] == 'first'
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    assert ledger._owner(filename)[0] == 'second'
