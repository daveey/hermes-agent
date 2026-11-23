"""Receipt signatures bind the exact payload and migration domain before I/O."""
import base64
import json

import pytest
import rfc8785
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hermes_cli import kanban_replay_migration as migration
from tests.hermes_cli import test_kanban_replay_migration as helpers
from tests.hermes_cli import test_kanban_replay_sender as sender

live = helpers.live


@pytest.mark.parametrize('defect', ['no_domain', 'wrong_domain', 'tampered_payload'])
def test_signature_binds_exact_payload_and_domain(live, defect):
    expected, payload, key = helpers.fixtures(live)
    domain = {'no_domain': b'', 'wrong_domain': b'hermes.other_protocol\x00',
              'tampered_payload': helpers.DOMAIN}[defect]
    signature = key.sign(domain + rfc8785.dumps(payload))
    if defect == 'tampered_payload':
        payload['expires_at_ms'] -= 1  # Still structurally valid and fresh, but not signed.
    receipt = rfc8785.dumps(dict(payload=payload, signature=base64.b64encode(signature).decode('ascii')))
    peer = sender.Receiver()
    before = list(live.iterdump())
    with pytest.raises(ValueError, match='unauthenticated'):
        migration.push(live, peer, 'default', 'macbook', primary=False,
                       expectation=expected, receipt=receipt, now_ms=2000,
                       public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    assert peer.calls == []
    assert list(live.iterdump()) == before


@pytest.mark.parametrize('field', ['issued_at_ms', 'expires_at_ms', 'readback_position', 'barriers'])
def test_signed_payload_missing_readiness_field_refuses(live, field):
    expected, payload, key = helpers.fixtures(live)
    del payload[field]
    before = list(live.iterdump())
    with pytest.raises(ValueError, match='identity or inventory mismatch'):
        helpers.send(live, expected, payload, key)
    assert list(live.iterdump()) == before


@pytest.mark.parametrize('public_key', [b'', b'short', None, 3])
def test_invalid_pinned_key_refuses_before_io(live, public_key):
    expected, payload, key = helpers.fixtures(live)
    peer = sender.Receiver()
    with pytest.raises(ValueError, match='unauthenticated'):
        migration.push(live, peer, 'default', 'macbook', primary=False,
                       expectation=expected, receipt=helpers.signed(payload, key),
                       now_ms=2000, public_key=public_key)
    assert peer.calls == []


def test_barrier_at_readback_and_max_lifetime_boundary_is_valid(live):
    expected, payload, key = helpers.fixtures(live)
    payload['barriers'][-1]['position'] = payload['readback_position']
    encoded = helpers.signed(payload, key)
    assert json.loads(encoded)['payload']['expires_at_ms'] - payload['issued_at_ms'] == 60000
    peer, result = helpers.send(live, expected, payload, key, receipt=encoded, now_ms=payload['issued_at_ms'])
    assert peer.calls[:3] == ['capability', 'inspect', 'session']
    assert result.sent == 0
