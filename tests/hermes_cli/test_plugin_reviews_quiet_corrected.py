"""Enroll independently reviewed corrective closures; fixtures are never executed."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import plugin_review_records as records

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/plugin_reviews/quiet_corrected'
PROVENANCE = json.loads((FIXTURES / 'provenance.json').read_text())['plugins']


def enrolled():
    data = (ROOT / records.REGISTRY).read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return records.read_records(ROOT, blob)


def materialize(key, root):
    info = PROVENANCE[key]
    source = FIXTURES / key
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    assert actual == {name + '.json' for name in info['files']}
    for name, digest in info['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        payload = json.loads((source / (name + '.json')).read_text())
        assert set(payload) == {'source_base64'}
        data = base64.b64decode(payload['source_base64'], validate=True)
        assert hashlib.sha256(data).hexdigest() == digest
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return info['files']


def test_corrective_source_receipts_are_enrolled():
    assert set(PROVENANCE) == {'hermes-monitor', 'kanban-db-hint'}
    assert sum(len(p['files']) for p in PROVENANCE.values()) == 12
    assert set(PROVENANCE) <= set(enrolled())
    assert PROVENANCE['hermes-monitor']['review_task'] == 't_01a21f0aee8b998b'
    assert PROVENANCE['kanban-db-hint']['review_task'] == 't_01a21e98d9614899'


@pytest.mark.parametrize('key', sorted(PROVENANCE))
def test_corrective_closure_validates(key, tmp_path):
    digests = materialize(key, tmp_path)
    assert enrolled()[key] == digests
    records.validate_files(tmp_path, enrolled()[key])


@pytest.mark.parametrize('key', sorted(PROVENANCE))
def test_every_corrective_file_tamper_refuses(key, tmp_path):
    digests = materialize(key, tmp_path)
    assert enrolled()[key] == digests
    for name in digests:
        path = tmp_path / name
        original = path.read_bytes()
        try:
            path.write_bytes(original + b'!')
            with pytest.raises(ValueError, match='digest mismatch'):
                records.validate_files(tmp_path, enrolled()[key])
        finally:
            path.write_bytes(original)
    records.validate_files(tmp_path, enrolled()[key])


@pytest.mark.parametrize('key', sorted(PROVENANCE))
def test_every_corrective_missing_file_refuses(key, tmp_path):
    digests = materialize(key, tmp_path)
    assert enrolled()[key] == digests
    for name in digests:
        path = tmp_path / name
        original = path.read_bytes()
        try:
            path.unlink()
            with pytest.raises(FileNotFoundError):
                records.validate_files(tmp_path, enrolled()[key])
        finally:
            path.write_bytes(original)


def test_corrective_registry_remains_boot_pinned():
    with pytest.raises(ValueError, match='boot-pinned'):
        records.read_records(ROOT, '0' * 40)
