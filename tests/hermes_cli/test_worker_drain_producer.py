"""A producer may not learn exemptions from arbitrary running processes."""
from __future__ import annotations

import json
import os

import pytest

from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence
from hermes_cli import worker_drain_baseline as producer
from tests.hermes_cli.test_worker_drain_baseline import process

pytestmark = pytest.mark.platforms('linux')


@pytest.fixture
def image(tmp_path, monkeypatch):
    proc = tmp_path / 'proc'
    proc.mkdir()
    process(proc)
    ident = fence.Identity('i-12345678', 'boot-a', 'epoch-a', os.getuid())
    monkeypatch.setattr(fence, 'load_activation', dict)
    monkeypatch.setattr(fence, 'current_identity', lambda act: ident)
    manifest = {'version': 1, 'units': [{'name': 'idle.service', 'kind': 'infrastructure',
                 'fragment_sha256': 'fragment-hash', 'exe_sha256': 'exe-hash'}]}
    monkeypatch.setattr(producer, 'load_manifest', lambda path: manifest)
    monkeypatch.setattr(producer, 'unit_state', lambda unit: {
        'MainPID': '10', 'ControlGroup': '/system.slice/idle.service', 'FragmentPath': '/unit'})
    monkeypatch.setattr(producer, 'file_digest', lambda path: 'fragment-hash' if str(path) == '/unit' else 'exe-hash')
    return proc, ident, manifest


def capture(image):
    proc, ident, _ = image
    return producer.capture('manifest', ident, proc_root=str(proc))


def test_declared_mainpid_produces_strict_idle_identity(image, monkeypatch):
    proc, _, _ = image
    doc = capture(image)
    assert doc['version'] == 2 and doc['boot_id'] == 'boot-a' and doc['iid'] == 'i-12345678'
    assert doc['idle'][0]['pid'] == 10 and doc['idle'][0]['start_ticks'] == 100
    monkeypatch.setattr(census, 'baseline_is_live', lambda base: True)
    base = census.Baseline(doc['boot_id'], doc['iid'], doc['node_epoch'],
                           frozenset(census._entry_identity(e) for e in doc['idle']))
    assert census.observe_processes(str(proc), base).state == census.ZERO
    process(proc, pid=11)
    assert census.observe_processes(str(proc), base).state == census.POSITIVE


def test_undeclared_same_exe_process_refuses_capture(image):
    process(image[0], pid=11)
    with pytest.raises(producer.CaptureRefused, match='undeclared'):
        capture(image)


@pytest.mark.parametrize('field,value', [('MainPID', '11'), ('ControlGroup', '/other'), ('FragmentPath', '/different')])
def test_forged_or_changed_unit_provenance_refuses(image, monkeypatch, field, value):
    state = {'MainPID': '10', 'ControlGroup': '/system.slice/idle.service', 'FragmentPath': '/unit'}
    monkeypatch.setattr(producer, 'unit_state', lambda unit: {**state, field: value})
    with pytest.raises(producer.CaptureRefused):
        capture(image)


def test_hermes_without_live_capability_is_never_exempt(image):
    image[2]['units'][0]['kind'] = 'hermes'
    with pytest.raises(producer.CaptureRefused, match='capability'):
        capture(image)


def test_unreadable_process_never_learns_baseline(image):
    (image[0] / '10' / 'exe').unlink()
    with pytest.raises(producer.CaptureRefused):
        capture(image)


def test_atomic_writer_owner_mode_and_failed_capture_invalidates(tmp_path, monkeypatch):
    # Exercise bytes/mode/replace without needing root; privilege refusal has its
    # own control and the production writer always uses fchown(0, 0).
    path = tmp_path / 'baseline.json'
    monkeypatch.setattr(producer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(producer, '_trusted_parent', lambda path: os.open(path.parent, os.O_DIRECTORY), raising=False)
    chowns = []
    monkeypatch.setattr(producer.os, 'fchown', lambda fd, uid, gid: chowns.append((uid, gid)))
    producer.write_baseline(path, {'version': 2})
    assert json.loads(path.read_text()) == {'version': 2}
    assert path.stat().st_mode & 0o777 == 0o644 and chowns == [(0, 0)]
    monkeypatch.setattr(producer, 'capture', lambda *a, **k: (_ for _ in ()).throw(producer.CaptureRefused('busy')))
    with pytest.raises(producer.CaptureRefused):
        producer.install('manifest', None, path=path)
    assert not path.exists()


def test_writer_refuses_unprivileged(tmp_path, monkeypatch):
    monkeypatch.setattr(producer.os, 'geteuid', lambda: 1000)
    with pytest.raises(producer.CaptureRefused, match='root'):
        producer.write_baseline(tmp_path / 'baseline', {})
