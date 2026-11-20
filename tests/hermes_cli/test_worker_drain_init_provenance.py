"""PID1 provenance is authored package content, not a nonexistent unit fragment."""
import json
import os

import pytest

from hermes_cli import worker_drain_baseline as producer
from hermes_cli import worker_drain_fence as fence
from tests.hermes_cli.test_worker_drain_baseline import process

pytestmark = pytest.mark.platforms('linux')


@pytest.mark.parametrize('changes', [{}, {'name': 'other.service'}, {'user': 'somebody'},
                                     {'package_path': 'relative'}, {'package_path': '/a/../b'}])
def test_init_manifest_has_explicit_package_contract(monkeypatch, changes):
    unit = {'name': 'init.scope', 'kind': 'init', 'package_path': '/image/systemd-package',
            'package_sha256': '0' * 64, 'exe_sha256': '1' * 64, **changes}
    monkeypatch.setattr(fence, '_secure_read', lambda *a: json.dumps({'version': 1, 'units': [unit]}))
    if changes:
        with pytest.raises(producer.CaptureRefused, match='untrusted'):
            producer.load_manifest('/manifest')
    else:
        assert producer.load_manifest('/manifest')['units'] == [unit]


def test_package_pinned_init_does_not_need_fragment(tmp_path, monkeypatch):
    process(tmp_path, pid=1, exe='/usr/lib/systemd/systemd')
    package = tmp_path / 'package-manifest'
    package.write_bytes(b'image package systemd=1\n')
    unit = {'name': 'init.scope', 'kind': 'init', 'package_path': str(package),
            'package_sha256': producer.file_digest(package), 'exe_sha256': 'exe-hash'}
    digest = producer.file_digest
    monkeypatch.setattr(producer, 'file_digest', lambda p: 'exe-hash' if str(p).endswith('/1/exe') else digest(p))
    state = {'MainPID': '0', 'ControlGroup': '/system.slice/idle.service', 'FragmentPath': ''}
    entry = producer._unit_entry(unit, state, str(tmp_path), fence.Identity('iid', 'boot', 'epoch'), {})
    assert entry['pid'] == 1 and entry['uid'] == os.getuid()
    package.write_bytes(b'changed package\n')
    with pytest.raises(producer.CaptureRefused, match='provenance'):
        producer._unit_entry(unit, state, str(tmp_path), fence.Identity('iid', 'boot', 'epoch'), {})


def test_init_cannot_be_declared_as_arbitrary_service(tmp_path):
    unit = {'name': 'arbitrary.service', 'kind': 'init', 'package_path': '/x',
            'package_sha256': '0' * 64, 'exe_sha256': '1' * 64}
    with pytest.raises(producer.CaptureRefused, match='provenance'):
        producer._unit_entry(unit, {'MainPID': '10', 'FragmentPath': '/x'}, str(tmp_path), None, {})
