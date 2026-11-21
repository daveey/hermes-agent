"""Startup must not decode the same catalog/config once per enabled plugin."""
from hermes_cli import plugin_catalog as catalog
from hermes_cli import plugins


def test_discovery_resolves_offline_removals_once(monkeypatch):
    manager = plugins.PluginManager()
    reads = []
    monkeypatch.setattr(catalog, '_stale_live_cache', lambda path: reads.append(path) or {'removed': []})
    monkeypatch.setattr(catalog, 'load_removed_list', list)
    monkeypatch.setattr(manager, '_refresh_secret_sources_after_discovery', lambda: None)
    monkeypatch.setattr(manager, '_discover_and_load_inner', lambda: [catalog.cached_removed_entries() for _ in range(20)])
    manager.discover_and_load()
    assert len(reads) == 1
    manager.discover_and_load(force=True)
    assert len(reads) == 2


def test_scope_file_changes_and_copy_isolation(tmp_path, monkeypatch):
    import json
    import os
    live = tmp_path / 'live.json'
    tree = tmp_path / 'tree'
    tree.mkdir()
    removed = tree / 'removed.yaml'
    removed.write_text('removed: [{name: tree-old}]')
    live.write_text(json.dumps({'removed': [{'name': 'live-old'}]}))
    monkeypatch.setattr(catalog, '_live_cache_path', lambda: live)
    monkeypatch.setattr(catalog, 'get_catalog_dir', lambda: tree)

    def names():
        return [e.name for e in catalog.cached_removed_entries()]

    with catalog.removal_read_scope():
        assert names() == ['tree-old', 'live-old']
        catalog.cached_removed_entries()[0].name = 'corrupted'
        assert names() == ['tree-old', 'live-old']
        old_time = live.stat().st_mtime_ns
        replacement = tmp_path / 'next.json'
        replacement.write_text(json.dumps({'removed': [{'name': 'live-new'}]}))
        os.utime(replacement, ns=(old_time, old_time))
        replacement.replace(live)
        assert names() == ['tree-old', 'live-new']
        removed.write_text('removed: [{name: tree-new}]')
        assert names() == ['tree-new', 'live-new']
        live.unlink()
        assert names() == ['tree-new']
        live.write_text(json.dumps({'removed': [{'name': 'live-added'}]}))
        assert names() == ['tree-new', 'live-added']


def test_scope_restores_on_exception_and_nested_profiles(tmp_path, monkeypatch):
    import pytest
    root = [tmp_path / 'one']
    other = tmp_path / 'two'
    for directory, name in ((root[0], 'one'), (other, 'two')):
        directory.mkdir()
        (directory / 'removed.yaml').write_text('removed: [{name: ' + name + '}]')
    monkeypatch.setattr(catalog, 'get_catalog_dir', lambda: root[0])
    monkeypatch.setattr(catalog, '_live_cache_path', lambda: root[0] / 'missing.json')
    with pytest.raises(RuntimeError, match='broken sweep'):
        with catalog.removal_read_scope():
            assert catalog.cached_removed_entries()[0].name == 'one'
            with catalog.removal_read_scope():
                root[0] = other
                assert catalog.cached_removed_entries()[0].name == 'two'
            assert catalog.cached_removed_entries()[0].name == 'two'
            raise RuntimeError('broken sweep')
    assert catalog._removal_reads.get() is None
    (other / 'removed.yaml').write_text('removed: [{name: fresh}]')
    assert catalog.cached_removed_entries()[0].name == 'fresh'
