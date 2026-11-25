"""Causal controls for independent validation, publication and private wiring."""
import asyncio
import json
import threading

import pytest

from gateway import code_provenance_control as pc
from hermes_cli import code_attestation as ca
from tests.gateway import test_code_provenance_control as helpers

runtime = helpers.runtime
ledger = helpers.ledger
pytestmark = pytest.mark.platforms('linux', 'macos')


@pytest.mark.parametrize('section', [{'unknowns': None}, 'not-a-section'])
def test_section_type_and_membership_are_independent(section):
    with pytest.raises(ValueError, match='invalid_page'):
        pc._validate({'schema': 1, 'op': 'page', 'snapshot_id': 'a' * 64,
                      'section': section, 'offset': 0, 'limit': 1})


def test_null_envelope_identity_is_supported():
    pc.validate_envelope({'protocol': 1, 'id': None})


def test_envelope_validation_is_scoped_to_provenance(runtime):
    async def scenario():
        server, _control, _params = await helpers.start(runtime)
        server.private_handlers['disposable-private'] = lambda params, peer: {'peer': peer}
        try:
            request = {'protocol': 1, 'verb': 'disposable-private', 'id': 'x' * 129, 'params': {}}
            reply = json.loads(server.handle_request_line(json.dumps(request).encode(), 'bound-peer'))
            assert reply['ok'] and reply['result'] == {'peer': 'bound-peer'}
            request['verb'] = 'code-provenance'
            reply = json.loads(server.handle_request_line(json.dumps(request).encode(), 'bound-peer'))
            assert not reply['ok'] and 'invalid_provenance_envelope' in reply['error']
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_capture_identity_change_without_ledger_replacement(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    original = ca.diagnostic_snapshot
    def changed(**kwargs):
        result = original(**kwargs)
        runtime[1].session_runtime_descriptor['instance_id'] = 'new-instance'
        return result
    monkeypatch.setattr(ca, 'diagnostic_snapshot', changed)
    assert control.handle(params, 'peer') == pc._error('identity_changed')
    assert not control._snapshots


@pytest.mark.parametrize('exception,expected', [
    (RuntimeError('entry_too_large'), 'RuntimeError'),
    (ValueError('SECRET_SENTINEL'), 'ValueError'),
])
def test_exception_class_and_message_whitelists_are_independent(runtime, monkeypatch, exception, expected):
    control = pc.CodeProvenanceControl(runtime[1])
    def fail(**kwargs):
        raise exception
    monkeypatch.setattr(ca, 'diagnostic_snapshot', fail)
    assert control.handle({'schema': 1, 'op': 'capture', 'expected': control.identity(),
                           'refresh_inventory': True}, 'peer') == pc._error(expected)


def test_publish_prunes_only_expired_other_peer_snapshots(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    control.handle(params, 'expired')
    control.handle(params, 'live')
    control._snapshots['expired']['expires'] = 0
    live = control._snapshots['live']
    control.handle(params, 'new')
    assert set(control._snapshots) == {'live', 'new'}
    assert control._snapshots['live'] is live


def test_wait_false_refuses_even_before_clock_deadline(runtime, monkeypatch):
    control = pc.CodeProvenanceControl(runtime[1])
    params = {'schema': 1, 'op': 'capture', 'expected': control.identity(), 'refresh_inventory': True}
    entered, release = threading.Event(), threading.Event()
    original_event = pc.threading.Event
    original_snapshot = ca.diagnostic_snapshot
    class EarlyWait:
        def __init__(self):
            self.event = original_event()
        def wait(self, timeout):
            assert entered.wait(2)
            return False
        def set(self):
            self.event.set()
        def is_set(self):
            return self.event.is_set()
    # Patch only the first Event allocation; Thread's internal Event stays real.
    count = 0
    def event():
        nonlocal count
        count += 1
        return EarlyWait() if count == 1 else original_event()
    def blocked(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_snapshot(**kwargs)
    monkeypatch.setattr(pc.threading, 'Event', event)
    monkeypatch.setattr(ca, 'diagnostic_snapshot', blocked)
    try:
        assert control.handle(params, 'peer') == pc._error('capture_timeout')
        assert pc._FLIGHT.locked()
    finally:
        release.set()
        for worker in threading.enumerate():
            if worker.name == 'gateway-code-provenance':
                worker.join(2)
    assert not pc._FLIGHT.locked() and not control._snapshots
