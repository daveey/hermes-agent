"""Exact independent source approvals, not PR verdicts or installed bytes."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import plugin_review_records as records

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/plugin_reviews/quiet_mhg40'
PROVENANCE = json.loads((FIXTURES / 'provenance.json').read_text())
PLUGINS = PROVENANCE['plugins']
FILES = [(key, name) for key, entry in PLUGINS.items() for name in entry['files']]


def enrolled():
    data = (ROOT / records.REGISTRY).read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    return records.read_records(ROOT, blob)


def materialize(key, root):
    info = PLUGINS[key]
    source = FIXTURES / key
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    assert actual == {part for group in info['fixture_parts'].values() for part in group}
    for name, digest in info['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        chunks = []
        for part in info['fixture_parts'][name]:
            assert not Path(part).is_absolute() and '..' not in Path(part).parts
            payload = json.loads((source / part).read_text())
            assert set(payload) == {'source_base64'}
            chunks.append(payload['source_base64'])
        data = base64.b64decode(''.join(chunks), validate=True)
        assert hashlib.sha256(data).hexdigest() == digest
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return info['files']


def test_delta_source_receipts_are_enrolled():
    assert set(PLUGINS) == {'mac-heavy-guard'}
    assert len(FILES) == 13
    assert PROVENANCE['review_task'] == 't_01a22117f2ca486c'
    assert PROVENANCE['source_commit'] == '8ae73075dfcb83f32d51b3ad686c261cf96d42be'
    assert PROVENANCE['report_sha256'] == '752f13193cf391d183cf7034ee065537fac3da41a090281e5241a13557a1796b'
    assert set(PLUGINS) <= set(enrolled())


@pytest.mark.parametrize('key', sorted(PLUGINS))
def test_delta_closure_validates(key, tmp_path):
    digests = materialize(key, tmp_path)
    assert PLUGINS[key]['verdict'] == 'approve'
    assert enrolled()[key] == digests
    records.validate_files(tmp_path, enrolled()[key])


@pytest.mark.parametrize('key,name', FILES)
def test_each_delta_file_tamper_refuses(key, name, tmp_path):
    materialize(key, tmp_path)
    path = tmp_path / name
    path.write_bytes(path.read_bytes() + b'!')
    with pytest.raises(ValueError, match='digest mismatch'):
        records.validate_files(tmp_path, enrolled()[key])


@pytest.mark.parametrize('key,name', FILES)
def test_each_delta_file_missing_refuses(key, name, tmp_path):
    materialize(key, tmp_path)
    (tmp_path / name).unlink()
    with pytest.raises(FileNotFoundError):
        records.validate_files(tmp_path, enrolled()[key])


def test_delta_registry_stays_boot_pinned():
    with pytest.raises(ValueError, match='boot-pinned'):
        records.read_records(ROOT, '0' * 40)


def test_unapproved_sibling_sources_are_not_enrolled():
    assert not {'ha-critical-gate', 'pool-llm', 'service-owner-guard', 'core-update'} & set(enrolled())



def test_source_scope_preserves_separately_managed_boundaries_and_limits():
    info = PLUGINS['mac-heavy-guard']
    assert 'UNVERIFIED' in info['installed_layout_binding']
    assert 'C1 partial' in info['residual_findings']
    assert 'MHG-N2..N5' in info['residual_findings']
    assert info['closure_file_map']
    assert any(row['status'] == 'trusted_separately' for row in info['executable_closure'])
    assert 'tailnet' not in PLUGINS and 'kanban-worker-nice' not in PLUGINS
