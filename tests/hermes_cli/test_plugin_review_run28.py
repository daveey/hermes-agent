"""Frozen RV3 bytes, not installed plugins, are the enrollment oracle.

Fixtures are inert .txt copies; no Ask Eve plugin is imported or invoked.
"""
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import plugin_review_records as reviews

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/plugin_reviews/run28'
MANIFEST_SHA = '7d30ad1e318a714a3d20e506a79e9ac28ea9de464f237fb644a2847a422464d0'
PLUGIN_FILES = {
    'askeve-github-read': ('__init__.py', 'credentials.py', 'github_read.py', 'plugin.yaml'),
    'project-work': ('__init__.py', 'client.py', 'config.py', 'errors.py', 'ingress.py',
                     'native.py', 'plugin.yaml', 'runtime.py', 'service.py', 'store.py'),
}


def _records():
    data = (ROOT / reviews.REGISTRY).read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return reviews.read_records(ROOT, blob)


def _materialize(tmp_path, key):
    root = tmp_path / key
    root.mkdir()
    for name in PLUGIN_FILES[key]:
        (root / name).write_bytes((FIXTURE / key / (name + '.txt')).read_bytes())
    return root


def test_run28_records_match_frozen_manifest_and_rv3():
    manifest = (FIXTURE / 'SHA256SUMS.plugins').read_text()
    rows = dict(line.split('  ', 1)[::-1] for line in manifest.splitlines())
    records = _records()
    provenance = json.loads((FIXTURE / 'provenance.json').read_text())
    assert set(provenance) == set(PLUGIN_FILES)
    for key, names in PLUGIN_FILES.items():
        assert provenance[key] == {
            'manifest_sha256': MANIFEST_SHA,
            'approval': 'deleg_8e533dd0',
            'approval_task': 't_01a1fd2c65f894e3',
            'approval_comment': 237105,
            'prefix': f'app-port/vendor/plugins/{key}/',
        }
        prefix = provenance[key]['prefix']
        expected = {path.removeprefix(prefix): digest for path, digest in rows.items()
                    if path.startswith(prefix)}
        assert set(expected) == set(names)
        assert records[key] == expected
        assert {p.name.removesuffix('.txt') for p in (FIXTURE / key).iterdir()} == set(names)
        for name, digest in expected.items():
            assert hashlib.sha256((FIXTURE / key / (name + '.txt')).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('key', PLUGIN_FILES)
def test_run28_validates_frozen_bytes(tmp_path, key):
    reviews.validate_files(_materialize(tmp_path, key), _records()[key])


@pytest.mark.parametrize('key,name', [(key, name) for key, names in PLUGIN_FILES.items() for name in names])
def test_run28_one_changed_byte_fails(tmp_path, key, name):
    root = _materialize(tmp_path, key)
    path = root / name
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ValueError, match='review source digest mismatch'):
        reviews.validate_files(root, _records()[key])
