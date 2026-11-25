"""Unmasked causal controls for bounded projection, without adjacent refusals."""
import time
from types import SimpleNamespace

import pytest

from hermes_cli import code_attestation_snapshot as projection
from tests.hermes_cli import test_code_attestation_snapshot as helpers

ledger = helpers.ledger


def test_launcher_registry_and_each_executed_distribution_are_present(ledger):
    ledger._launcher['module'] = 'trusted-launcher'
    ledger._units[0]['producer_distribution'] = {'dist': 'first'}
    ledger._dep_units = {'second': {'producer_distribution': {'dist': 'second'}}}
    result = helpers.capture()
    assert result.boot['boot_registry_status'] == 'recorded'
    assert {'kind': 'launcher', 'identity': {'kind': 'trusted_launcher_script',
            'module': 'trusted-launcher'}} in helpers.values(result, 'metadata')
    assert {row['producer_distribution']['dist'] for row in helpers.values(result, 'executed_units')} == {'first', 'second'}


def test_scalar_types_and_null_are_whitelisted_individually():
    value = {'filename': None, 'component': 'core', 'generation': 1, 'trust': True,
             'source_sha256': object()}
    assert projection.scalars(value, projection.UNIT_KEYS) == {
        'filename': None, 'component': 'core', 'generation': 1, 'trust': True}


def test_preflight_raw_length_guard_has_no_escaped_size_mask(monkeypatch):
    # Small ASCII chunks alone fit; only whole-input bound may reject before encoding.
    monkeypatch.setattr(projection, 'MAX_ENTRY_BYTES', 5)
    monkeypatch.setattr(projection.ENCODER, 'encode', lambda value: '"x"')
    with pytest.raises(ValueError, match='entry_too_large'):
        projection._preflight('123456', time.monotonic() + 2)


def test_preflight_deadline_independent_of_builder(monkeypatch):
    monkeypatch.setattr(projection.time, 'monotonic', lambda: 10)
    with pytest.raises(TimeoutError, match='snapshot_deadline'):
        projection._preflight('short', 5)


@pytest.mark.parametrize('mode', ['deadline', 'entry', 'total'])
def test_iterencode_limits_without_preflight_mask(monkeypatch, mode):
    monkeypatch.setattr(projection, '_preflight', lambda *args: None)
    builder = projection._Builder(5 if mode == 'deadline' else time.monotonic() + 2)
    if mode == 'deadline':
        monkeypatch.setattr(projection.time, 'monotonic', lambda: 10)
    elif mode == 'entry':
        monkeypatch.setattr(projection, 'MAX_ENTRY_BYTES', 2)
    else:
        monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', 4097)
    with pytest.raises(TimeoutError if mode == 'deadline' else ValueError,
                       match='snapshot_deadline' if mode == 'deadline' else ('entry_too_large' if mode == 'entry' else 'snapshot_too_large')):
        builder.add('metadata', {'x': 'safe'})
    assert not builder.rows['metadata']


def test_finish_counts_boot_and_final_deadline_separately(monkeypatch):
    builder = projection._Builder(time.monotonic() + 2)
    # isolate finish from add's independently tested guards
    monkeypatch.setattr(builder, 'add', lambda *args: None)
    monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', 4097)
    with pytest.raises(ValueError, match='snapshot_too_large'):
        builder.finish(1, 0, {}, {'x': 'boot'})
    monkeypatch.setattr(projection, 'MAX_SNAPSHOT_BYTES', 16000000)
    builder.deadline = 0
    with pytest.raises(TimeoutError, match='snapshot_deadline'):
        builder.finish(1, 0, {}, {})


def test_post_inventory_deadline_prevents_later_projection(ledger, monkeypatch):
    ticks = [0]
    monkeypatch.setattr(projection, 'disk_identity', lambda *args: {})
    monkeypatch.setattr(projection.time, 'monotonic', lambda: ticks[0])
    monkeypatch.setattr(ledger, '_inventory_modules', lambda **kw: ticks.__setitem__(0, 10))
    monkeypatch.setattr(projection, '_Builder', lambda *args: pytest.fail('projected after inventory deadline'))
    with pytest.raises(TimeoutError, match='snapshot_deadline'):
        projection.snapshot(ledger, deadline=5)


def test_review_registry_is_verified_before_export(ledger, monkeypatch):
    from hermes_cli import plugin_review_records
    ledger._reviewed_plugins = {'plugin': SimpleNamespace()}
    def verify(actual):
        assert actual is ledger
        actual.mark_unknown('plugin-review-unverified:captured')
    monkeypatch.setattr(plugin_review_records, 'verify_registered', verify)
    assert 'plugin-review-unverified:captured' in helpers.values(helpers.capture(), 'unknowns')
