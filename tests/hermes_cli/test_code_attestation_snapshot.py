"""Bounded metadata projection of an installed ledger, not a new trust source."""
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation as ca


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    a = object.__new__(ca.Attestation)
    a.core_root, a.boot_sha = tmp_path, 'a' * 40
    a.route, a.stub_head_digest = None, None
    a._pid, a._lock = __import__('os').getpid(), threading.RLock()
    a._generation = 7
    a._unknown = {'bootstrap-provenance-pending', 'extension', 'blocking'}
    a._extension_unknowns, a._blocking_unknowns = {'extension', 'blocking'}, {'blocking'}
    a._components = {'plugin:x': (tmp_path / 'plugin', 'plugin', {'a.py': 'b' * 64})}
    a._blobs = {'hermes_cli/data/plugin-review-records.json': 'c' * 40}
    a._deps = SimpleNamespace(roots=(tmp_path / 'site-packages',), dists={
        (tmp_path / 'site-packages', 'x-1'): {'record_sha256': 'd' * 64,
                                             'installer_sha256': None, 'used': True,
                                             'secret': 'SECRET_SENTINEL'}}, limit=False)
    a._units = [{'component': 'plugin:x', 'source_sha256': 'e' * 64, 'secret': 'SECRET_SENTINEL'}]
    a._dep_units, a._native_loads = {}, []
    a._unknown_sources, a._unknown_detail, a._generators = {}, {}, {}
    a._seed = [{'module': 'seed', 'source_sha256': 'f' * 64, 'secret': 'SECRET_SENTINEL'}]
    a._launcher = {'kind': 'trusted_launcher_script', 'launch_handoff_env': 'SECRET_SENTINEL'}
    a.interpreter_identity = {'executable': '/python', 'version': 'test', 'secret': 'SECRET_SENTINEL'}
    monkeypatch.setattr(a, '_inventory_modules', lambda **kw: None)
    monkeypatch.setattr(ca, '_installed', a)
    return a


def capture():
    return ca.diagnostic_snapshot(deadline=time.monotonic() + 2)


def values(snapshot, section):
    return [json.loads(row) for row in snapshot.sections[section]]


def test_missing_ledger_does_not_install(monkeypatch):
    monkeypatch.setattr(ca, '_installed', None)
    with pytest.raises(RuntimeError, match='not installed'):
        capture()
    assert ca._installed is None


def test_complete_partition_roots_receipts_and_hashes(ledger):
    snapshot = capture()
    assert values(snapshot, 'unknowns') == sorted(ledger._unknown)
    assert values(snapshot, 'quiet_extension') == ['extension']
    assert values(snapshot, 'quiet_blocking') == ['blocking', 'bootstrap-provenance-pending']
    assert {'kind': 'reviewed_file', 'component': 'plugin:x', 'path': 'a.py', 'sha256': 'b' * 64} in values(snapshot, 'components')
    receipts = values(snapshot, 'dependencies')
    assert any(r.get('distribution') == 'x-1' and r['root'] == str(ledger._deps.roots[0]) for r in receipts)
    for section, rows in snapshot.sections.items():
        encoded = b'[' + b','.join(rows) + b']'
        assert snapshot.manifest[section] == {'count': len(rows), 'sha256': hashlib.sha256(encoded).hexdigest()}
    assert snapshot.generation == 7
    assert snapshot.full_provenance is False
    assert 'SECRET_SENTINEL' not in repr(snapshot)


def test_frozen_capture_and_no_unknown_clearing(ledger):
    before = set(ledger._unknown)
    first = capture()
    ledger.mark_unknown('new-refusal')
    second = capture()
    assert values(first, 'unknowns') == sorted(before)
    assert values(second, 'unknowns') == sorted(before | {'new-refusal'})
    assert ledger._unknown == before | {'new-refusal'}


@pytest.mark.parametrize('reason', ['unexpected-prehook:x', 'unsupported-route:other',
    'generated-origin-unknown:x', 'plugin-review-unverified:x', 'dependency-record-skew:x',
    'dependency-record-ambiguous:x', 'dependency-root-loss:x'])
def test_refusals_survive_exactly(ledger, reason):
    ledger.mark_unknown(reason)
    assert reason in values(capture(), 'unknowns')
    assert reason in ledger.latched_unknowns()


def test_inventory_deadline_and_errors_are_not_partial_success(ledger, monkeypatch):
    def fail(**kwargs):
        ledger.mark_unknown('monotone-inventory-refusal')
        raise OSError('SECRET_SENTINEL')
    monkeypatch.setattr(ledger, '_inventory_modules', fail)
    with pytest.raises(OSError):
        capture()
    assert 'monotone-inventory-refusal' in ledger._unknown
    with pytest.raises(TimeoutError):
        ca.diagnostic_snapshot(deadline=time.monotonic() - 1)


def test_incremental_total_and_entry_caps(ledger, monkeypatch):
    from hermes_cli import code_attestation_snapshot as projection
    ledger._unknown = {'x' * 300000}
    with pytest.raises(ValueError, match='entry_too_large'):
        capture()
    ledger._unknown = {str(i) + 'x' * 100 for i in range(100)}
    monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', 1000)
    with pytest.raises(ValueError, match='snapshot_too_large'):
        capture()


def test_boot_identity_is_recorded_not_current_files(ledger):
    snapshot = capture()
    metadata = values(snapshot, 'metadata')
    boot = next(r for r in metadata if r['kind'] == 'ledger')
    assert boot['boot_sha'] == ledger.boot_sha
    assert boot['core_root'] == str(ledger.core_root)
    assert boot['boot_registry_blob'] == 'c' * 40
    assert any(r['kind'] == 'history' and r['status'] == 'unavailable' for r in metadata)


def test_lock_wait_is_bounded(ledger):
    ready, release = threading.Event(), threading.Event()
    def hold():
        with ledger._lock:
            ready.set()
            assert release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(2)
    try:
        with pytest.raises(TimeoutError):
            ca.diagnostic_snapshot(deadline=time.monotonic() + .02)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()


def test_preflight_rejects_escaped_scalar_before_whole_encoding(ledger, monkeypatch):
    from hermes_cli import code_attestation_snapshot as projection
    original = projection.ENCODER.iterencode
    def guarded(value, **kwargs):
        assert not (type(value) is str and len(value) > 1024)
        return original(value, **kwargs)
    monkeypatch.setattr(projection.ENCODER, 'iterencode', guarded)
    ledger._unknown = {'\u0000' * 50000}
    with pytest.raises(ValueError, match='entry_too_large'):
        capture()


def test_root_qualified_receipts_preserve_unused_and_overlapping_names(ledger):
    root = Path('/second/site-packages')
    ledger._deps.roots += (root,)
    ledger._deps.dists[(root, 'x-1')] = {'record_sha256': '2' * 64, 'used': False}
    receipts = [r for r in values(capture(), 'dependencies') if r['kind'] == 'installer_receipt']
    assert {(r['root'], r['record_sha256'], r['used']) for r in receipts} == {
        (str(root), '2' * 64, False), (str(ledger._deps.roots[0]), 'd' * 64, True)}


def test_actual_hook_cold_capture_consistent_and_never_authorizes(tmp_path):
    from tests.hermes_cli.test_code_attestation import run_probe
    result = run_probe(tmp_path, '''
        from hermes_cli import code_attestation as ca
        ca._installed = a
        assert 'hermes_cli.code_attestation_snapshot' not in sys.modules
        a.mark_unknown('unsupported-route:diagnostic-control')
        s = ca.diagnostic_snapshot(deadline=time.monotonic() + 2)
        raw = [json.loads(r) for r in s.sections['unknowns']]
        after = sorted(a._unknown)
        generation = a._generation
        from gateway.capsule_contract import owner_launch_sha
        from hermes_state_runtime import RuntimeStoreError
        try:
            owner_launch_sha(True)
            allowed = True
        except RuntimeStoreError:
            allowed = False
        print(json.dumps({'raw': raw, 'after': after, 'captured_generation': s.generation,
                          'generation': generation, 'allowed': allowed}))
    ''')
    assert result['raw'] == result['after']
    assert result['captured_generation'] == result['generation']
    assert result['allowed'] is False


def test_projection_metadata_whitelist_and_immutable_manifest(ledger):
    from hermes_cli import code_attestation_snapshot as projection
    ledger._dep_units = {'unit': {'filename': '/retained/unit.py', 'producer_distribution':
        {'dist': 'safe', 'secret': 'SECRET_SENTINEL'}, 'code': (lambda: 'SECRET_SENTINEL').__code__}}
    ledger._units[0]['producer_distribution'] = {'dist': 'also-safe', 'secret': 'SECRET_SENTINEL'}
    ledger._native_loads = [{'name': '/native.so', 'caller_component': 'core', 'secret': 'SECRET_SENTINEL'}]
    ledger._generators = {'g': SimpleNamespace(name='safe-generator', source_sha256='1' * 64,
        kind='generator', distribution={'dist': 'safe', 'secret': 'SECRET_SENTINEL'})}
    ledger._unknown_sources = {'extension': [{'filename': '/known.py', 'source_sha256': '2' * 64,
        'secret': 'SECRET_SENTINEL', 'frame_locals': {'secret': 'SECRET_SENTINEL'}}]}
    ledger._unknown_detail = {'extension': {'safe-producer'}}
    ledger._seed.append({'module': 'known', 'git_blob': '3' * 40, 'secret': 'SECRET_SENTINEL'})
    snapshot = capture()
    assert 'SECRET_SENTINEL' not in repr(snapshot)
    assert any(row.get('distribution', {}).get('dist') == 'safe' for row in values(snapshot, 'producers'))
    assert any(row.get('producer_distribution', {}).get('dist') == 'safe' for row in values(snapshot, 'executed_units'))
    metadata = values(snapshot, 'metadata')
    assert {'kind': 'capture', 'generation': snapshot.generation, 'begin_mono': snapshot.begin_mono,
            'end_mono': snapshot.end_mono} in metadata
    assert {'kind': 'disk', **snapshot.disk} in metadata
    assert dict(snapshot.boot) in metadata
    with pytest.raises(TypeError):
        snapshot.manifest['unknowns']['count'] = 0
    with pytest.raises(ValueError, match='unsupported_metadata_value'):
        projection._Builder(time.monotonic() + 1).add('metadata', {'unsupported': object()})


def test_metadata_missing_registry_and_launcher_remains_unavailable(ledger):
    ledger._launcher = None
    ledger._blobs.clear()
    snapshot = capture()
    assert snapshot.boot['boot_registry_blob'] is None
    assert snapshot.boot['boot_registry_status'] == 'unavailable'
    assert not any(row['kind'] == 'launcher' for row in values(snapshot, 'metadata'))
