"""Startup must not decode the same catalog/config once per enabled plugin."""
import json
from pathlib import Path

import pytest

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


@pytest.mark.parametrize('source', ['live', 'tree'])
def test_failed_read_retried_within_scope(tmp_path, monkeypatch, source):
    from hermes_cli.plugins_cmd_catalog import installed_plugin_removal

    tree = tmp_path / 'catalog'
    tree.mkdir()
    live = tmp_path / 'live.json'
    removed = tree / 'removed.yaml'
    live.write_text(json.dumps({'removed': [{'name': 'recalled'}] if source == 'live' else []}))
    removed.write_text('removed: [{name: recalled}]' if source == 'tree' else 'removed: []')
    monkeypatch.setattr(catalog, '_live_cache_path', lambda: live)
    monkeypatch.setattr(catalog, 'get_catalog_dir', lambda: tree)
    target = live if source == 'live' else removed
    original = Path.read_text
    failures = []

    def flaky_read(path, *args, **kwargs):
        if path == target and not failures:
            failures.append(path)
            raise OSError('transient catalog read failure')
        return original(path, *args, **kwargs)

    # Hold the generation constant: recovery need not rewrite the file.
    monkeypatch.setattr(catalog, '_removal_version', lambda path: ('unchanged',))
    monkeypatch.setattr(Path, 'read_text', flaky_read)
    with catalog.removal_read_scope():
        assert installed_plugin_removal('ordinary', tmp_path) is None
        assert installed_plugin_removal('recalled', tmp_path) is not None
        assert installed_plugin_removal('recalled', tmp_path) is not None
    assert failures == [target]


@pytest.mark.parametrize('source', ['live', 'tree'])
def test_failed_stat_is_not_memoized(tmp_path, monkeypatch, source):
    live = tmp_path / 'live.json'
    tree = tmp_path / 'tree'
    tree.mkdir()
    live.write_text(json.dumps({'removed': []}))
    (tree / 'removed.yaml').write_text('removed: []')
    monkeypatch.setattr(catalog, '_live_cache_path', lambda: live)
    monkeypatch.setattr(catalog, 'get_catalog_dir', lambda: tree)
    target = live if source == 'live' else tree / 'removed.yaml'
    monkeypatch.setattr(catalog, '_removal_version', lambda path: None if path == target else ('stable',))
    with catalog.removal_read_scope():
        assert catalog.cached_removed_entries() == []
        assert catalog._removal_reads.get() == {}


@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('failure', [PermissionError, OSError])
def test_tree_reader_stat_failure_retries(tmp_path, monkeypatch, scoped, failure):
    from contextlib import nullcontext
    from hermes_cli.plugins_cmd_catalog import installed_plugin_removal

    tree = tmp_path / 'catalog'
    tree.mkdir()
    removed = tree / 'removed.yaml'
    removed.write_text('removed: [{name: recalled}]')
    monkeypatch.setattr(catalog, 'get_catalog_dir', lambda: tree)
    monkeypatch.setattr(catalog, '_live_cache_path', lambda: tmp_path / 'absent.json')
    import os
    original = os.stat
    calls = []
    fail_at = 2 if scoped else 1  # after the scope's successful fingerprint stat

    def flaky_stat(path, *args, **kwargs):
        if Path(path) == removed:
            calls.append(removed)
            if len(calls) == fail_at:
                raise failure('transient tree metadata failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, 'stat', flaky_stat)
    with catalog.removal_read_scope() if scoped else nullcontext():
        assert installed_plugin_removal('ordinary', tmp_path) is None
        assert len(calls) == fail_at, 'fault must be injected'
        assert installed_plugin_removal('recalled', tmp_path) is not None
        assert installed_plugin_removal('recalled', tmp_path) is not None


def test_missing_catalogs_are_memoized(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, '_live_cache_path', lambda: tmp_path / 'missing.json')
    monkeypatch.setattr(catalog, 'get_catalog_dir', lambda: tmp_path)
    original = catalog._stale_live_cache
    reads = []

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(catalog, '_stale_live_cache', read)
    with catalog.removal_read_scope():
        assert catalog.cached_removed_entries() == []
        assert catalog.cached_removed_entries() == []
    assert len(reads) == 1
