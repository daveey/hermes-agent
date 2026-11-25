"""Every secondary reply parser contains decoder limits, not application errors."""
import pytest

from hermes_cli import kanban_sync_attachment_http as attachments
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_hub_transport as transport


@pytest.mark.parametrize('failure', [RecursionError, ValueError])
def test_attachment_reply_decoder_failure_is_typed(monkeypatch, failure):
    raw = b'fixture-reply'
    monkeypatch.setattr(attachments, '_request', lambda *args, **kwargs: raw)
    original = transport.json.loads
    error = failure('decoder resource limit')

    def decode(value, *args, **kwargs):
        if value == raw:
            raise error
        return original(value, *args, **kwargs)

    monkeypatch.setattr(transport.json, 'loads', decode)
    with pytest.raises(client.HubError, match='invalid JSON response') as caught:
        attachments.upload(None, 'default', 'attachment', 'digest', b'payload')
    assert caught.value.status == 502
    assert caught.value.__cause__ is error


@pytest.mark.parametrize('failure', [RecursionError, ValueError])
def test_session_conflict_reply_keeps_original_fault(monkeypatch, failure):
    hub = client.Hub(client.Identity('http://127.0.0.1:1', 'fixture-key', 'macbook'))
    raw = 'fixture-conflict'
    refusal = client.HubError(409, raw)
    original = transport.json.loads

    def call(*args, **kwargs):
        raise refusal

    def decode(value, *args, **kwargs):
        if value == raw:
            raise failure('decoder resource limit')
        return original(value, *args, **kwargs)

    monkeypatch.setattr(hub, '_call', call)
    monkeypatch.setattr(transport.json, 'loads', decode)
    try:
        with pytest.raises(client.HubError) as caught:
            hub.sessions('default', {})
        assert caught.value is refusal
    finally:
        hub.close()


def test_attachment_application_valueerror_is_not_a_reply_fault(monkeypatch):
    error = ValueError('request construction failed')

    def request(*args, **kwargs):
        raise error

    monkeypatch.setattr(attachments, '_request', request)
    with pytest.raises(ValueError) as caught:
        attachments.upload(None, 'default', 'attachment', 'digest', b'payload')
    assert caught.value is error


def test_attachment_empty_reply_remains_invalid(monkeypatch):
    monkeypatch.setattr(attachments, '_request', lambda *args, **kwargs: b'')
    with pytest.raises(client.HubError, match='invalid JSON response'):
        attachments.upload(None, 'default', 'attachment', 'digest', b'payload')
