"""Fail-closed review registry parsing and registration seams."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import plugin_review_records as records


def put(tmp_path, payload):
    path = tmp_path / records.REGISTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload.encode()
    path.write_bytes(data)
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


@pytest.mark.parametrize('payload', [
    '[]', '{"version":true,"plugins":{}}', '{"version":2,"plugins":{}}',
    '{"version":1,"plugins":{},"extra":0}', '{"version":1,"plugins":{},"plugins":{}}',
    '{"version":1,"plugins":{"plug":{}}}', '{"version":1,"plugins":[]}',
    '{"version":1,"plugins":{"plug":{"__init__.py":null}}}',
])
def test_invalid_schema(tmp_path, payload):
    blob = put(tmp_path, payload)
    with pytest.raises(ValueError):
        records.read_records(tmp_path, blob)


@pytest.mark.parametrize('name', ['../file.py', '/file.py', './file.py', 'a//b.py', 'a\\b.py', 'C:file.py', 'a\x00.py'])
def test_invalid_path(tmp_path, name):
    blob = put(tmp_path, json.dumps({'version': 1, 'plugins': {'plug': {name: 'a' * 64}}}))
    with pytest.raises(ValueError):
        records.read_records(tmp_path, blob)


def test_pinned_registry_and_limits(tmp_path, monkeypatch):
    payload = json.dumps({'version': 1, 'plugins': {'plug': {'file.py': 'a' * 64}}})
    blob = put(tmp_path, payload)
    assert records.read_records(tmp_path, blob)['plug'] == {'file.py': 'a' * 64}
    for wrong in (None, '0' * 40):
        with pytest.raises(ValueError):
            records.read_records(tmp_path, wrong)
    monkeypatch.setattr(records, 'MAX_FILES', 0)
    with pytest.raises(ValueError, match='count'):
        records.read_records(tmp_path, blob)
    monkeypatch.setattr(records, 'MAX_REGISTRY', 1)
    with pytest.raises(ValueError, match='limit'):
        records.read_records(tmp_path, blob)


def test_file_bounds_and_escape(tmp_path, monkeypatch):
    root = tmp_path / 'plugin'
    root.mkdir()
    source = root / 'file.py'
    source.write_bytes(b'abc')
    digest = hashlib.sha256(b'abc').hexdigest()
    records.validate_files(root, {'file.py': digest})
    with pytest.raises(ValueError, match='mismatch'):
        records.validate_files(root, {'file.py': '0' * 64})
    outside = tmp_path / 'outside.py'
    outside.write_bytes(b'abc')
    source.unlink()
    source.symlink_to(outside)
    with pytest.raises(ValueError, match='escapes'):
        records.validate_files(root, {'file.py': digest})
    source.unlink()
    source.write_bytes(b'abc')
    monkeypatch.setattr(records, 'MAX_FILE_BYTES', 2)
    with pytest.raises(ValueError, match='limit'):
        records.validate_files(root, {'file.py': digest})


def test_unreviewed_entrypoint_latched_before_load(monkeypatch):
    from hermes_cli import code_attestation, plugins_loader
    events = []
    ledger = SimpleNamespace(mark_unknown=lambda reason: events.append(reason))
    monkeypatch.setattr(code_attestation, '_installed', ledger)
    entry = SimpleNamespace(name='example', load=lambda: events.append('load') or 17)
    monkeypatch.setattr(plugins_loader, '_select_entry_point_group', lambda *args: [entry])
    monkeypatch.setattr(plugins_loader.importlib.metadata, 'entry_points', list)
    manifest = SimpleNamespace(name='example', key='example')
    assert plugins_loader.PluginLoaderMixin()._load_entrypoint_module(manifest) == 17
    assert events == ['plugin-entrypoint-unreviewed:example', 'load']


def test_uninstalled_ledger_is_not_created(tmp_path, monkeypatch):
    from hermes_cli import code_attestation
    monkeypatch.setattr(code_attestation, '_installed', None)
    assert records.register_directory('example', tmp_path) is False
    records.unreviewed_entrypoint('example')
    assert code_attestation._installed is None


def test_physical_core_not_source_label(tmp_path, monkeypatch):
    from hermes_cli import code_attestation
    unknowns = []
    ledger = SimpleNamespace(core_root=tmp_path, _blobs={}, mark_unknown=unknowns.append)
    monkeypatch.setattr(code_attestation, '_installed', ledger)
    assert records.register_directory('example', tmp_path / 'bundled')
    assert not records.register_directory('example', Path(str(tmp_path) + '-outside'))
    assert unknowns == ['plugin-review-unverified:example']
