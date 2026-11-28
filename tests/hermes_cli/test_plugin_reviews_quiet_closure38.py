"""Exact independent source approvals, not PR verdicts or installed bytes."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import plugin_review_records as records

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/plugin_reviews/quiet_closure38'
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


def test_delta_source_receipts_are_enrolled():
    assert set(PLUGINS) == {'skill-outcomes'}
    assert len(FILES) == 6
    assert PROVENANCE['review_task'] == 't_01a220b317d44498'
    assert PROVENANCE['source_commit'] == '44943439ecc5e99382ea68a16f64f9c4ff44e2b5'
    assert PROVENANCE['report_sha256'] == 'a96c33911566fa1dfade2ffecd41d42fedaa2fcc9b25b5a669b0954e0b18aca6'
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


def test_transitive_source_receipt_does_not_approve_coach_or_installed_layout():
    info = PLUGINS['skill-outcomes']
    assert set(info['closure_file_map']) == {
        'plugins/coach/lessons.py', 'plugins/coach/self_heal.py',
        'plugins/coach/reply_check.py', 'plugins/coach/module_cache.py',
        'plugins/coach/placement_evidence.py',
    }
    for record in info['closure_file_map'].values():
        assert set(record) == {'git_blob', 'mode', 'bytes', 'sha256'}
        assert len(record['sha256']) == 64 and len(record['git_blob']) == 40
    assert 'UNVERIFIED' in info['installed_layout_binding']
    assert 'NOT APPROVED' in info['installed_layout_binding']
    assert 'coach' not in enrolled()
