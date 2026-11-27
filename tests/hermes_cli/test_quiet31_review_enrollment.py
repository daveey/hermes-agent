"""Exact-source enrollment, using inert bytes from five independent source reviews."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import plugin_review_records as records

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/plugin_reviews/quiet31'
PROVENANCE = json.loads((FIXTURES / 'provenance.json').read_text())['plugins']


def enrolled():
    data = (ROOT / records.REGISTRY).read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return records.read_records(ROOT, blob)


def materialize(key, root):
    """Decode inert per-file data; never import or invoke reviewed code."""
    info = PROVENANCE[key]
    fixture_root = FIXTURES / key
    files = {p.relative_to(fixture_root).as_posix() for p in fixture_root.rglob('*')
             if p.is_file()}
    assert files == {name + '.json' for name in info['files']}
    for name, digest in info['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        payload = json.loads((fixture_root / (name + '.json')).read_text())
        assert set(payload) == {'source_base64'}
        data = base64.b64decode(payload['source_base64'], validate=True)
        assert hashlib.sha256(data).hexdigest() == digest
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return info['files']


def test_review_map_pairing_is_enrolled():
    # The frozen review cohort is closed; unrelated future grants remain possible.
    assert len(PROVENANCE) == 31
    assert sum(len(p['files']) for p in PROVENANCE.values()) == 309
    assert set(PROVENANCE) <= set(enrolled())
    assert enrolled()['account-usage']['selection.py'] == (
        '84c8692078951ea86ac28044a230b3507fcf9761a46025452a882e80a44f95b2')
    assert {'system-one', 'prompt-coach', 'fast-rate-limit-fallback'} <= set(PROVENANCE)


@pytest.mark.parametrize('key', sorted(PROVENANCE))
def test_approved_frozen_files_validate(key, tmp_path):
    digests = materialize(key, tmp_path)
    assert enrolled()[key] == digests
    records.validate_files(tmp_path, enrolled()[key])


@pytest.mark.parametrize('key', sorted(PROVENANCE))
def test_every_enrolled_file_tamper_refuses(key, tmp_path):
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
