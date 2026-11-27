"""Registration must precede sibling aliases and newly adopted executable .pth files."""
import hashlib
import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation as ca, plugin_review_records as records
from hermes_cli import plugins
from hermes_cli.plugins import PluginManager


@pytest.mark.parametrize('reviewed', [True, False])
def test_discovery_registers_reviewed_sibling_before_first_alias(tmp_path, monkeypatch, reviewed):
    core = tmp_path / 'core'
    core.mkdir()
    sibling = tmp_path / 'sibling'
    sibling.mkdir()
    source = sibling / '__init__.py'
    source.write_text('VALUE = 42\n')
    digests = {'__init__.py': hashlib.sha256(source.read_bytes()).hexdigest()}
    registry = core / records.REGISTRY
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'version': 1, 'plugins': {'sibling': digests} if reviewed else {}}))
    data = registry.read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    ledger = ca.Attestation(core, '0' * 40)
    ledger._blobs[records.REGISTRY] = blob
    monkeypatch.setattr(ca, '_installed', ledger)
    manifests = [SimpleNamespace(key=key, name=key, path=str(path), source='user', kind='plugin', portable=False, requires_plugins=[])
                 for key, path in [('first', tmp_path / 'first'), ('sibling', sibling),
                                   ('disabled', tmp_path / 'disabled')]]
    manager = PluginManager()
    monkeypatch.setattr(manager, '_collect_directory_manifests', lambda: manifests)
    monkeypatch.setattr(manager, '_scan_entry_points', list)
    monkeypatch.setattr(plugins, '_get_enabled_plugins', lambda: {'first', 'sibling', 'disabled'})
    monkeypatch.setattr(plugins, '_get_disabled_plugins', lambda: {'disabled'})
    monkeypatch.setattr(manager, '_warn_python_dependencies', lambda m: None)
    monkeypatch.setattr(manager, '_validate_plugin_config_schema', lambda m: None)
    observed = []

    def load(manifest):
        if manifest.key != 'first':
            return
        observed.append('plugin:sibling' in ledger._components)
        assert 'plugin:disabled' not in ledger._components
        spec = importlib.util.spec_from_file_location('_private_sibling_alias', source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        # This is the sticky late-registration check, not a mocked ledger operation.
        records.register_directory('sibling', sibling)

    monkeypatch.setattr(manager, '_load_plugin', load)
    manager._discover_and_load_inner()
    assert observed == [reviewed]
    reasons = ledger.latched_unknowns()
    assert not any(r.startswith('pre-registration-module:') for r in reasons)
    assert ('plugin-review-unverified:sibling' in reasons) is not reviewed


@pytest.mark.parametrize('source,portable,kind,enabled,disabled,expected', [
    ('user', False, 'plugin', {'sibling'}, set(), True),
    ('project', False, 'plugin', {'sibling'}, set(), True),
    ('bundled', False, 'backend', set(), set(), True),
    ('bundled', False, 'platform', set(), set(), True),
    ('user', False, 'plugin', set(), set(), False),
    ('user', False, 'plugin', {'sibling'}, {'sibling'}, False),
    ('entrypoint', False, 'plugin', {'sibling'}, set(), False),
    ('user', True, 'plugin', {'sibling'}, set(), False),
    ('user', False, 'exclusive', {'sibling'}, set(), False),
])
def test_closure_keeps_existing_enablement_and_source_gates(
        tmp_path, monkeypatch, source, portable, kind, enabled, disabled, expected):
    from hermes_cli.plugins_loader import PluginLoaderMixin
    manifest = SimpleNamespace(key='sibling', name='sibling', path=str(tmp_path),
                               source=source, portable=portable, kind=kind)
    calls = []
    monkeypatch.setattr(records, 'register_directory', lambda *args: calls.append(args))
    PluginLoaderMixin()._register_reviewed_directory_closure([manifest], disabled, enabled)
    assert bool(calls) is expected


def test_closure_cannot_overwrite_another_profile_root(tmp_path, monkeypatch):
    from hermes_cli.plugins_loader import PluginLoaderMixin
    from threading import RLock
    root = tmp_path / 'new-profile'
    root.mkdir()
    (root / '__init__.py').write_text('')
    digest = hashlib.sha256(b'').hexdigest()
    digests = {'__init__.py': digest}
    unknowns = []
    foreign = (tmp_path / 'other-profile', 'plugin', digests)
    ledger = SimpleNamespace(core_root=tmp_path / 'core', _blobs={}, _lock=RLock(),
                             _components={'plugin:sibling': foreign}, mark_unknown=unknowns.append,
                             _settle_under=lambda root: None)
    ledger.register_component_root = lambda *args, **kwargs: ca.Attestation.register_component_root(
        ledger, *args, **kwargs)
    monkeypatch.setattr(ca, '_installed', ledger)
    monkeypatch.setattr(records, 'read_records', lambda *args: {'sibling': digests})
    manifest = SimpleNamespace(key='sibling', name='sibling', path=str(root),
                               source='user', portable=False, kind='plugin')
    PluginLoaderMixin()._register_reviewed_directory_closure([manifest], set(), {'sibling'})
    assert ledger._components == {'plugin:sibling': foreign}
    assert unknowns == ['component-root-conflict:plugin:sibling']
    assert not getattr(ledger, '_reviewed_plugins', {})


@pytest.mark.parametrize('loaded_attestation', [True, False])
def test_adopt_registers_before_path_and_pth(tmp_path, monkeypatch, loaded_attestation):
    from pm import environments_adopt as adopt
    from pm.environments import site_packages
    from hermes_cli import runtime_state

    previous, selected = tmp_path / 'old', tmp_path / 'new'
    old, new = site_packages(previous), site_packages(selected)
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (new / 'new.pth').write_text('import bootstrap_test\n')
    events = []
    monkeypatch.setattr(runtime_state, 'lease_generation', lambda env: events.append('lease'))
    monkeypatch.setattr(adopt, '_loaded_from_changed', lambda *args: [])
    monkeypatch.setattr(sys, 'path', [str(old)])

    def register(root):
        assert root == new
        assert sys.path == [str(old)]
        events.append('register')

    if loaded_attestation:
        monkeypatch.setitem(sys.modules, 'hermes_cli.code_attestation',
                            SimpleNamespace(register_dependency_environment=register))
    else:
        monkeypatch.delitem(sys.modules, 'hermes_cli.code_attestation', raising=False)

    def pth(*args):
        assert sys.path == [str(new)]
        assert events == (['lease', 'register'] if loaded_attestation else ['lease'])
        events.append('pth')

    monkeypatch.setattr(adopt.site, 'addpackage', pth)
    assert adopt.adopt(previous, selected, previous)
    assert events[-1] == 'pth'


@pytest.mark.parametrize('condition', ['foreign', 'unchanged', 'changed'])
def test_adopt_refusals_and_noop_do_not_register(tmp_path, monkeypatch, condition):
    from pm import environments_adopt as adopt
    from hermes_cli import runtime_state

    old, new = tmp_path / 'old', tmp_path / 'new'
    monkeypatch.setattr(runtime_state, 'lease_generation', lambda env: pytest.fail('leased'))
    monkeypatch.setattr(adopt, '_loaded_from_changed', lambda *args: ['loaded'])
    monkeypatch.setitem(sys.modules, 'hermes_cli.code_attestation', SimpleNamespace(
        register_dependency_environment=lambda root: pytest.fail('registered')))
    running = tmp_path / 'foreign' if condition == 'foreign' else old
    selected = old if condition == 'unchanged' else new
    assert adopt.adopt(old, selected, running) is (condition == 'unchanged')
