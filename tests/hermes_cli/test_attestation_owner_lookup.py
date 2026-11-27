"""Component classification must not build path ancestors for every plugin root."""
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from hermes_cli import code_attestation as ca


def test_owner_lookup_avoids_per_component_ancestor_expansion(monkeypatch):
    ledger = object.__new__(ca.Attestation)
    ledger._components = {
        f'plugin:{i}': (PurePosixPath(f'/plugins/plugin{i}'), 'plugin', {})
        for i in range(67)
    }
    path = PurePosixPath('/managed/site-packages/sdk/module.py')
    original = PurePosixPath.parents
    expansions = []

    def parents(self):
        expansions.append(self)
        return original.__get__(self)

    monkeypatch.setattr(PurePosixPath, 'parents', property(parents))
    assert ledger._owner_path(path) is None
    assert len(expansions) <= 1


@pytest.mark.parametrize('path_type,root,path,relative', [
    (PurePosixPath, '/plugins/a', '/plugins/a/mod.py', 'mod.py'),
    (PurePosixPath, '/plugins/a', '/plugins/ab/mod.py', None),
    (PurePosixPath, '/plugins/a', '/plugins/a', '.'),
    (PurePosixPath, '/', '/plugins/a/mod.py', 'plugins/a/mod.py'),
    (PurePosixPath, '.', '/plugins/a/mod.py', None),
    (PurePosixPath, '.', 'plugins/a/mod.py', 'plugins/a/mod.py'),
    (PurePosixPath, '//plugins/a', '/plugins/a/mod.py', None),
    (PureWindowsPath, 'C:/Plugins/A', 'c:/plugins/a/mod.py', 'mod.py'),
    (PureWindowsPath, 'C:/Plugins/A', 'D:/Plugins/A/mod.py', None),
    (PureWindowsPath, '//server/share/A', '//SERVER/SHARE/a/mod.py', 'mod.py'),
])
def test_owner_lookup_retains_pathlib_containment(path_type, root, path, relative):
    ledger = object.__new__(ca.Attestation)
    digests = {'mod.py': 'reviewed'}
    ledger._components = {'first': (path_type(root), 'plugin', digests)}
    found = ledger._owner_path(path_type(path))
    expected = None if relative is None else ('first', 'plugin', path_type(path), relative, digests)
    assert found == expected


@pytest.mark.parametrize('reverse', [False, True])
def test_owner_lookup_retains_first_registered_overlapping_root(reverse):
    ledger = object.__new__(ca.Attestation)
    entries = [('outer', (PurePosixPath('/plugins'), 'plugin', {})),
               ('inner', (PurePosixPath('/plugins/a'), 'plugin', {}))]
    if reverse:
        entries.reverse()
    ledger._components = dict(entries)
    found = ledger._owner_path(PurePosixPath('/plugins/a/mod.py'))
    assert found[0] == entries[0][0]
    # No persistent ownership cache may hide a changed component root.
    ledger._components = {'new': (PurePosixPath('/plugins/a'), 'plugin', {})}
    assert ledger._owner_path(PurePosixPath('/plugins/a/mod.py'))[0] == 'new'
