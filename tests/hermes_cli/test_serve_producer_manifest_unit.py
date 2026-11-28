"""Manifest v1 shape/digest rules (pure functions; the real-exec ordering lives in test_serve_producer_shared_broker)."""
import copy
import hashlib

import pytest

from hermes_cli.serve_producer_manifest import (
    MAX_ENTRIES, MAX_FILE, declared_files, verify_declared, verify_loaded,
)
from hermes_cli.serve_producer_protocol import ProtocolError

A, B = hashlib.sha256(b'a').hexdigest(), hashlib.sha256(b'b').hexdigest()


def base():
    return {'version': 1, 'providers': {'panel': {'path': 'provider.py', 'sha256': A}},
            'callbacks': {'done': [{'name': 'd', 'path': 'done.py', 'sha256': B}], 'waiting': []},
            'modes': {'links': 'off'}, 'facts': {'switch': True}}


def mutated(fn):
    value = copy.deepcopy(base())
    fn(value)
    return value


def test_valid_manifest_declares_every_file_once():
    assert declared_files(base()) == {'provider.py': A, 'done.py': B}
    same = mutated(lambda m: m['callbacks']['waiting'].append({'name': 'w', 'path': 'done.py', 'sha256': B}))
    assert declared_files(same) == {'provider.py': A, 'done.py': B}  # same file, same digest: one entry


@pytest.mark.parametrize('change', [
    lambda m: m.update(version=2),
    lambda m: m.update(version=True),
    lambda m: m.pop('version'),
    lambda m: m.update(providers={}),
    lambda m: m.update(providers=[]),
    lambda m: m.update(providers=['panel']),
    lambda m: m.update(providers={1: {'path': 'p.py', 'sha256': A}}),
    lambda m: m.pop('providers'),
    lambda m: m.update(providers={'': {'path': 'p.py', 'sha256': A}}),
    lambda m: m.update(providers={f'p{i}': {'path': f'p{i}.py', 'sha256': A} for i in range(MAX_ENTRIES + 1)}),
    lambda m: m['providers']['panel'].update(sha256='A' * 64),
    lambda m: m['providers']['panel'].update(sha256='a' * 63),
    lambda m: m['providers']['panel'].update(sha256=5),
    lambda m: m['providers']['panel'].update(path='/abs.py'),
    lambda m: m['providers']['panel'].update(path='../up.py'),
    lambda m: m['providers']['panel'].update(path=''),
    lambda m: m['providers']['panel'].update(path=5),
    lambda m: m['providers'].update(panel='provider.py'),
    lambda m: m.update(callbacks={'done': []}),
    lambda m: m.update(callbacks={'done': [], 'waiting': [], 'extra': []}),
    lambda m: m.update(callbacks=[]),
    lambda m: m.pop('callbacks'),
    lambda m: m['callbacks'].update(done={}),
    lambda m: m['callbacks'].update(done=[{'path': 'd.py', 'sha256': A}]),
    lambda m: m['callbacks'].update(done=[{'name': '', 'path': 'd.py', 'sha256': A}]),
    lambda m: m['callbacks'].update(done=['d.py']),
    lambda m: m['callbacks'].update(waiting=[{'name': 'w', 'path': 'w.py', 'sha256': A}] * (MAX_ENTRIES + 1)),
    lambda m: m['callbacks'].update(waiting=[{'name': 'w', 'path': 'done.py', 'sha256': A}]),  # conflicting digest
    lambda m: m.update(modes=[]),
    lambda m: m.pop('modes'),
    lambda m: m.update(modes={'a': None}),
    lambda m: m.update(modes={'a': 1.5}),
    lambda m: m.update(modes={'a': {'nested': 1}}),
    lambda m: m.update(modes={'': 'x'}),
    lambda m: m.update(modes={1: 'x'}),
    lambda m: m.update(modes={f'm{i}': 'x' for i in range(MAX_ENTRIES + 1)}),
    lambda m: m.update(facts=[]),
    lambda m: m.pop('facts'),
    lambda m: m.update(facts={'a': [1]}),
    lambda m: m.update(facts={f'f{i}': 1 for i in range(MAX_ENTRIES + 1)}),
])
def test_malformed_manifest_is_refused(change):
    with pytest.raises(ProtocolError):
        declared_files(mutated(change))


def test_manifest_must_be_a_dict_and_limits_are_inclusive():
    with pytest.raises(ProtocolError):
        declared_files([])
    full = mutated(lambda m: m.update(
        providers={f'p{i}': {'path': f'p{i}.py', 'sha256': A} for i in range(MAX_ENTRIES)},
        modes={f'm{i}': 'x' for i in range(MAX_ENTRIES)}, facts={f'f{i}': True for i in range(MAX_ENTRIES)}))
    full['callbacks']['waiting'] = [{'name': f'w{i}', 'path': f'w{i}.py', 'sha256': A} for i in range(MAX_ENTRIES)]
    assert len(declared_files(full)) == 2 * MAX_ENTRIES + 1
    assert declared_files(mutated(lambda m: m.update(modes={'a': False, 'b': 0, 'c': 'x'})))


def _files(tmp_path):
    (tmp_path / 'provider.py').write_bytes(b'a')
    (tmp_path / 'done.py').write_bytes(b'b')


def test_verify_declared_checks_every_file_against_its_digest(tmp_path):
    _files(tmp_path)
    assert verify_declared(base(), tmp_path) == {'provider.py': A, 'done.py': B}
    (tmp_path / 'done.py').write_bytes(b'changed')
    with pytest.raises(ProtocolError, match='digest mismatch'):
        verify_declared(base(), tmp_path)
    (tmp_path / 'done.py').unlink()
    with pytest.raises(ProtocolError, match='member missing'):
        verify_declared(base(), tmp_path)
    (tmp_path / 'done.py').write_bytes(b'x' * (MAX_FILE + 1))
    with pytest.raises(ProtocolError, match='too large'):
        verify_declared(base(), tmp_path)
    (tmp_path / 'done.py').write_bytes(b'b')
    (tmp_path / 'provider.py').write_bytes(b'x' * MAX_FILE)  # the limit itself is readable, then fails digest
    with pytest.raises(ProtocolError, match='digest mismatch'):
        verify_declared(base(), tmp_path)


def _module(path):
    return type('Module', (), {'__file__': str(path)})


def test_verify_loaded_rejects_undeclared_and_mutated_members_only_inside_the_provider_dir(tmp_path):
    _files(tmp_path)
    outside = tmp_path.parent / 'outside_mod.py'
    outside.write_text('x')
    (tmp_path / 'extra.py').write_text('x')
    modules = {'provider': _module(tmp_path / 'provider.py'), 'done': _module(tmp_path / 'done.py'),
               'outside': _module(outside), 'builtin': type('Module', (), {'__file__': None}),
               'nofile': type('Module', (), {})}
    executed = {str((tmp_path / 'provider.py').resolve()): A,
                str((tmp_path / 'done.py').resolve()): B,
                str((tmp_path / 'extra.py').resolve()): 'entry-digest'}
    with pytest.raises(ProtocolError, match='executable bytes unverified'):
        verify_loaded(base(), tmp_path, modules=modules)
    # Pure post-import predicate fixture; real audit/loader proof is exercised separately.
    verify_loaded(base(), tmp_path, modules=modules, executed=executed)
    modules['extra'] = _module(tmp_path / 'extra.py')
    with pytest.raises(ProtocolError, match='undeclared'):
        verify_loaded(base(), tmp_path, modules=modules, executed=executed)
    verify_loaded(base(), tmp_path, allowed=('extra.py',), modules=modules, executed=executed)
    (tmp_path / 'extra.py').write_text('changed')
    verify_loaded(base(), tmp_path, allowed=('extra.py',), modules=modules, executed=executed)
    with pytest.raises(ProtocolError, match='manifest path'):
        verify_loaded(base(), tmp_path, allowed=('../extra.py',), modules=modules, executed=executed)
    del modules['extra']
    (tmp_path / 'done.py').write_bytes(b'tampered')
    with pytest.raises(ProtocolError, match='loaded provider module digest mismatch'):
        verify_loaded(base(), tmp_path, modules=modules, executed=executed)


def test_verify_loaded_defaults_to_the_live_module_table(tmp_path):
    _files(tmp_path)
    verify_loaded(base(), tmp_path)  # nothing live is loaded from this directory
