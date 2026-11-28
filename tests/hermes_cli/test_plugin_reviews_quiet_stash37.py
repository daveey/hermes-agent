"""Exact independent source approvals, not PR verdicts or installed bytes."""
import base64
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import plugin_review_records as records

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/plugin_reviews/quiet_stash37'
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
    assert set(PLUGINS) == {'stash-worktree-guard'}
    assert len(FILES) == 14
    assert PROVENANCE['review_task'] == 't_01a22039b040e865'
    assert PROVENANCE['source_commit'] == 'a362991f2b57e59e52d2e66c2b62ebc64e3a6ada'
    assert PROVENANCE['report_sha256'] == 'cf15fa4bf5d72d58ae334b28a32d3cf7e123b48ceead24adc93ac864bdbb2dd2'
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
    assert not {'ha-critical-gate', 'pool-llm', 'service-owner-guard', 'core-update', 'skill-outcomes'} & set(enrolled())
