"""Unmasked validation/custody boundary controls beside real-socket integration tests."""
import threading


import pytest

from gateway import code_provenance_control as pc
from hermes_cli import code_attestation as ca
from tests.gateway import test_code_provenance_control as helpers
from tests.hermes_cli import test_code_attestation_snapshot as snapshot_helpers

runtime = helpers.runtime
ledger = snapshot_helpers.ledger
pytestmark = pytest.mark.platforms('linux', 'macos')


@pytest.mark.parametrize('value', [None, [], 1, {'op': 'capture'}, {'schema': None}, {'schema': '1'}])
def test_invalid_schema_is_classified(value):
    with pytest.raises(ValueError, match='invalid_schema'):
        pc._validate(value)


@pytest.mark.parametrize('key,value', [('pid', 0), ('pid', 2**63), ('pid', 1.0),
    ('start_time', 0), ('start_time', 2**63), ('start_time', 1.0),
    ('instance_id', None), ('instance_id', 1), ('instance_id', 'x' * 129)])
def test_expected_fields_reject_unmasked_invalid_values(runtime, key, value):
    control = pc.CodeProvenanceControl(runtime[1])
    expected = {**control.identity(), key: value}
    with pytest.raises(ValueError, match='invalid_capture'):
        control.handle({'schema': 1, 'op': 'capture', 'expected': expected, 'refresh_inventory': True}, 'peer')


@pytest.mark.parametrize('key,value', [('snapshot_id', None), ('snapshot_id', 1),
    ('section', None), ('offset', 2**63), ('offset', '1'), ('limit', 1.0), ('limit', '1')])
def test_page_invalid_values_unmasked(key, value):
    params = {'schema': 1, 'op': 'page', 'snapshot_id': 'a' * 64,
              'section': 'unknowns', 'offset': 0, 'limit': 1, key: value}
    with pytest.raises(ValueError, match='invalid_page'):
        pc._validate(params)


def test_authentication_identity_and_ledger_process_refusals(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    with pytest.raises(PermissionError, match='authenticated_peer_required'):
        control.handle(params, None)
    runtime[2]._pid += 1
    assert control.handle(params, 'peer') == pc._error('ledger_process_mismatch')
    runtime[2]._pid -= 1
    monkeypatch.setattr(pc.os, 'getpid', lambda: control.pid + 1)
    assert control.handle(params, 'peer') == pc._error('process_identity_unavailable')


def test_missing_start_identity_refuses(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    control.start_time = None
    assert control.handle(params, 'peer') == pc._error('process_identity_unavailable')


def test_thread_start_failure_releases_global_flight(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    original = pc.threading.Thread.start
    def fail(thread):
        raise RuntimeError('SECRET_SENTINEL')
    monkeypatch.setattr(pc.threading.Thread, 'start', fail)
    assert control.handle(params, 'peer') == pc._error('worker_unavailable')
    assert not pc._FLIGHT.locked()
    monkeypatch.setattr(pc.threading.Thread, 'start', original)
    assert control.handle(params, 'peer')['complete']


def test_replaced_ledger_during_capture_cannot_publish(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    original = ca.diagnostic_snapshot
    def replace(**kwargs):
        snapshot = original(**kwargs)
        monkeypatch.setattr(ca, '_installed', None)
        return snapshot
    monkeypatch.setattr(ca, 'diagnostic_snapshot', replace)
    assert control.handle(params, 'peer') == pc._error('identity_changed')
    assert not control._snapshots


def test_page_ledger_replacement_and_empty_exhausted_page(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    summary = control.handle(params, 'peer')
    exhausted = control.handle(helpers.page_params(summary, offset=summary['raw']['count']), 'peer')
    assert exhausted['entries'] == [] and exhausted['next'] is None
    monkeypatch.setattr(ca, '_installed', None)
    assert control.handle(helpers.page_params(summary), 'peer') == pc._error('snapshot_identity_changed')


def test_single_flight_includes_publication(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    entered, release = threading.Event(), threading.Event()
    original = control._publish
    result = []
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(control, '_publish', blocked)
    worker = threading.Thread(target=lambda: result.append(control.handle(params, 'peer')))
    worker.start()
    assert entered.wait(2)
    try:
        assert control.handle(params, 'peer') == pc._error('capture_busy')
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and result[0]['complete']


def test_page_has_count_and_encoded_byte_ceiling(runtime):
    a = runtime[2]
    a._unknown = {f'{i:04d}' for i in range(300)}
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    summary = control.handle(params, 'peer')
    page = control.handle(helpers.page_params(summary), 'peer')
    assert len(page['entries']) == 128 and page['next'] == 128
    a._unknown = {f'{i:04d}' + 'x' * 130000 for i in range(3)}
    summary = control.handle(params, 'peer')
    page = control.handle(helpers.page_params(summary), 'peer')
    assert len(page['entries']) == 1 and page['next'] == 1


def test_page_guard_cannot_truncate_an_oversized_only_entry(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    summary = control.handle(params, 'peer')
    monkeypatch.setattr(pc, 'MAX_PAGE_BYTES', 1)
    assert control.handle(helpers.page_params(summary), 'peer') == pc._error('entry_too_large')


def test_completed_after_deadline_is_not_a_reusable_positive(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    original = control._publish
    def fail_publish(*args):
        pytest.fail('late result published')
    monkeypatch.setattr(control, '_publish', fail_publish)

    # Zero budget is only a deterministic boundary control, not a production override.
    monkeypatch.setattr(pc, 'CAPTURE_SECONDS', 0)
    assert control.handle(params, 'peer') == pc._error('capture_timeout')
    # Join the actual worker before restoring mocks or starting a new request.
    for thread in threading.enumerate():
        if thread.name == 'gateway-code-provenance':
            thread.join(2)
    assert not pc._FLIGHT.locked()
    monkeypatch.setattr(pc, 'CAPTURE_SECONDS', 2)
    monkeypatch.setattr(control, '_publish', original)
    assert control.handle(params, 'peer')['complete']
