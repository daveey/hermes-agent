"""Compose real manifest parsing, capture, writer, loader and live revalidation."""
import json
import os
from types import SimpleNamespace

import pytest

from hermes_cli import worker_drain_baseline as producer
from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence
from tests.hermes_cli.test_worker_drain_baseline import process

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def composed(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    exe = tmp_path / 'image-executable'
    exe.write_bytes(b'reviewed image executable')
    fragment = tmp_path / 'idle.service'
    fragment.write_text('[Service]\nExecStart=/image/idled\n')
    process(proc, exe=str(exe))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'units': [{
        'name': 'idle.service', 'kind': 'infrastructure',
        'fragment_sha256': producer.file_digest(fragment),
        'exe_sha256': producer.file_digest(exe)}]}))
    manifest.chmod(0o644)
    ident = fence.Identity('i-12345678', 'boot-a', 'epoch-a', os.getuid())
    monkeypatch.setattr(fence, 'load_activation', dict)
    monkeypatch.setattr(fence, 'current_identity', lambda act: ident)
    monkeypatch.setattr(producer, 'MANIFEST_PATH', manifest)
    monkeypatch.setattr(producer, 'unit_state', lambda unit: {
        'MainPID': '10', 'ControlGroup': '/system.slice/idle.service', 'FragmentPath': str(fragment)})
    # Only the system-manager and host-path boundaries are routed into the
    # fixture. All manifest/digest/identity/schema decisions are production code.
    validate = producer.validate_baseline
    monkeypatch.setattr(producer, 'validate_baseline', lambda base, ident: validate(base, ident, proc_root=str(proc)))
    real_fstat = os.fstat
    def root_stat(fd):
        st = real_fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=st.st_mode, st_nlink=st.st_nlink,
                               st_size=st.st_size, st_gid=st.st_gid)
    monkeypatch.setattr(os, 'fstat', root_stat)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'fchown', lambda *a: None)
    monkeypatch.setattr(producer, '_trusted_parent', lambda path: os.open(path.parent, os.O_DIRECTORY))
    path = tmp_path / 'baseline.json'
    producer.write_baseline(path, producer.capture(manifest, ident, proc_root=str(proc)))
    return proc, path, exe, fragment


def test_capture_load_idle_extra_process_and_reboot(composed, monkeypatch):
    proc, path, _, _ = composed
    base = census.load_baseline(str(path))
    assert base is not None
    assert census.observe_processes(str(proc), base).state == census.ZERO
    process(proc, pid=11)
    assert census.observe_processes(str(proc), base).state == census.POSITIVE
    monkeypatch.setattr(fence, 'current_identity', lambda act: fence.Identity('i-12345678', 'new-boot', 'epoch-a'))
    assert census.load_baseline(str(path)) is None
    assert census.observe_processes(str(proc), base).state == census.UNKNOWN


@pytest.mark.parametrize('index', [2, 3])
def test_live_package_mutation_invalidates_persisted_baseline(composed, index):
    _, path, _, _ = composed
    base = census.load_baseline(str(path))
    assert base is not None
    composed[index].write_bytes(b'changed package')
    assert census.load_baseline(str(path)) is None
    assert not census.baseline_is_live(base)
