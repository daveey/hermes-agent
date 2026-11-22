"""Exercise the actual urllib reader and credential/redirect fences offline."""
import io
import json
import urllib.error
import urllib.request

import pytest

from hermes_cli import kanban_card_answer_transport as transport
from hermes_cli.kanban_card_answer import AnswerRefused


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(dict(gateway='owner', hub_url='https://example.com/hub')))
    (tmp_path / 'gateway.key').write_text('fixture-key')
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(path))
    return path


def test_reads_exact_authenticated_record(config, monkeypatch):
    reader = transport.Records()
    def open_request(req, timeout):
        assert req.full_url == 'https://example.com/hub/v2/sync/requests/request-one'
        assert req.method == 'GET' and req.headers['Authorization'] == 'Bearer fixture-key'
        assert req.get_header('User-agent') == 'hermes-card-answer-readback/1.0'
        assert timeout == 10
        return io.BytesIO(json.dumps({'request': {'id': 'request-one', 'state': 'done'}}).encode())
    monkeypatch.setattr(reader._opener, 'open', open_request)
    assert reader.read('request-one') == {'id': 'request-one', 'state': 'done'}


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:secret@example.com',
                                  'https://example.com/?q=a', 'https://example.com/#fragment'])
def test_endpoint_never_accepts_credentials_or_plaintext(config, url):
    config.write_text(json.dumps(dict(gateway='owner', hub_url=url)))
    with pytest.raises(AnswerRefused, match='hub_endpoint_invalid'):
        transport.Records()


@pytest.mark.parametrize('rid', ['../other', 'a/b', '', 'a?x=1', 'a' * 201])
def test_untrusted_id_cannot_change_route(config, rid, monkeypatch):
    reader = transport.Records()
    monkeypatch.setattr(reader._opener, 'open', lambda *a, **kw: pytest.fail('network must not run'))
    with pytest.raises(AnswerRefused, match='courier_request_id_invalid'):
        reader.read(rid)


@pytest.mark.parametrize('raw', [b'not JSON', b'[]', b'{"request":{"id":"other"}}', b'x' * (1024 * 1024 + 1)],
                         ids=['invalid-json', 'array', 'wrong-id', 'oversize'])
def test_malformed_or_wrong_record_refuses(config, raw, monkeypatch):
    reader = transport.Records()
    monkeypatch.setattr(reader._opener, 'open', lambda *a, **kw: io.BytesIO(raw))
    with pytest.raises(AnswerRefused, match='courier_record_invalid'):
        reader.read('request-one')


@pytest.mark.parametrize('status', [302, 401, 403, 404, 503])
def test_http_errors_have_no_sensitive_details(config, status, monkeypatch):
    reader = transport.Records()
    def fail(*a, **kw):
        raise urllib.error.HTTPError('https://private.example', status, 'private-secret', {}, None)
    monkeypatch.setattr(reader._opener, 'open', fail)
    with pytest.raises((OSError, AnswerRefused)) as exc:
        reader.read('request-one')
    assert 'private' not in str(exc.value)
    assert str(exc.value) == ('courier_record_missing' if status == 404 else 'courier_record_unavailable')


def test_redirect_never_forwards_enrolled_key():
    request = urllib.request.Request('https://example.com', headers={'Authorization': 'Bearer fixture-key'})
    assert transport._NoRedirect().redirect_request(request, None, 302, '', {}, 'https://other.example') is None
